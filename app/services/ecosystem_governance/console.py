# ruff: file-ignore[line-too-long]
"""Ecosystem console read models (Epic 79 story 79.1, FR-405).

Unified, masked views over owner facades: Installations (Apps, connectors, API keys), delegated authorisations, webhooks,
MCP sessions. Never reads secrets, webhook payloads or Copilot / portal conversation text. Owner data that is absent /
renamed degrades gracefully (empty rows / zero counts) rather than raising.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.core.permissions import SENSITIVE_PERMISSIONS
from app.models.api_keys import ApiKey
from app.models.ecosystem_governance import AccessReview, AccessReviewItem
from app.models.oauth import AppInstallation, AppUserAuthorization, OAuthToken
from app.schemas.ecosystem_governance import ConsoleSummary, InstallationRow, ViolationRow

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext


def _sensitive(scopes: list[str]) -> list[str]:
    return [s for s in scopes if s in SENSITIVE_PERMISSIONS or ":write" in s or ":manage" in s]


async def _kill_switch_on(ctx: RequestContext, db: AsyncSession) -> bool:
    try:
        from app.services import ecosystem_service

        state = await ecosystem_service.state(ctx, db)
        return bool(getattr(state, "kill_switch_on", False) or getattr(state, "suspended", False))
    except Exception:  # pragma: no cover - degrade to "off"
        return False


async def summary(ctx: RequestContext, db: AsyncSession) -> ConsoleSummary:
    tid = ctx.tenant_id
    by_status: dict[str, int] = {}
    rows = await db.execute(
        select(AppInstallation.status, func.count())
        .where(AppInstallation.tenant_id == tid)
        .group_by(AppInstallation.status)
    )
    for status, count in rows:
        by_status[status] = int(count)
    pending = (
        await db.execute(
            select(func.count())
            .select_from(AppInstallation)
            .where(
                AppInstallation.tenant_id == tid,
                AppInstallation.pending_manifest_version.isnot(None),
            )
        )
    ).scalar_one()
    open_items = (
        await db.execute(
            select(func.count())
            .select_from(AccessReviewItem)
            .join(AccessReview, AccessReview.id == AccessReviewItem.review_id)
            .where(
                AccessReviewItem.tenant_id == tid,
                AccessReview.status == "open",
                AccessReviewItem.decision == "pending",
            )
        )
    ).scalar_one()
    return ConsoleSummary(
        active=by_status.get("active", 0),
        degraded=by_status.get("degraded", 0),
        suspended=by_status.get("suspended", 0),
        pending_reconsents=int(pending),
        failing_subscriptions=await _failing_subscriptions(ctx, db),
        active_mcp_sessions=await _active_mcp_sessions(ctx, db),
        open_access_review_items=int(open_items),
        violations_7d=0,
        kill_switch_on=await _kill_switch_on(ctx, db),
    )


async def _failing_subscriptions(ctx: RequestContext, db: AsyncSession) -> int:
    try:
        from app.services import webhooks_service

        subs = await webhooks_service.list_subscriptions(ctx, db)  # type: ignore[call-arg]
        return sum(1 for s in subs if getattr(s, "status", None) in ("failing", "disabled", "auto_disabled"))
    except Exception:  # pragma: no cover
        return 0


async def _active_mcp_sessions(ctx: RequestContext, db: AsyncSession) -> int:  # ruff: ignore[unused-function-argument]
    try:
        res = await db.execute(select(func.count()).select_from(_mcp_sessions_table()).where(_mcp_active_clause()))
        return int(res.scalar_one())
    except Exception:  # pragma: no cover
        return 0


def _mcp_sessions_table() -> Any:
    from sqlalchemy import table

    return table("mcp_sessions")


def _mcp_active_clause() -> Any:
    from sqlalchemy import column, literal

    return column("status") == literal("active")


async def installations(
    ctx: RequestContext, db: AsyncSession, *, kind: str | None = None, status: str | None = None, q: str | None = None
) -> list[InstallationRow]:
    rows: list[InstallationRow] = []
    if kind in (None, "app", "connector"):
        where = [AppInstallation.tenant_id == ctx.tenant_id, AppInstallation.status != "uninstalled"]
        if kind in ("app", "connector"):
            where.append(AppInstallation.kind == kind)
        if status:
            where.append(AppInstallation.status == status)
        if q:
            where.append(AppInstallation.display_name.ilike(f"%{q}%"))
        insts = (await db.execute(select(AppInstallation).where(*where))).scalars().all()
        for inst in insts:
            scopes = list(inst.granted_scopes or [])
            rows.append(
                InstallationRow(
                    kind=inst.kind,
                    id=str(inst.id),
                    name=inst.display_name,
                    publisher=("platform" if inst.kind == "connector" else "partner"),
                    version=str(inst.manifest_version),
                    granted_scopes=scopes,
                    sensitive_scopes=_sensitive(scopes),
                    status=inst.status,
                    health_status=inst.health_status,
                    last_activity_at=inst.last_error_at or inst.health_checked_at,
                    accountable_user=str(inst.accountable_user_id) if inst.accountable_user_id else None,
                )
            )
    if kind in (None, "api_key"):
        kwhere = [ApiKey.tenant_id == ctx.tenant_id, ApiKey.revoked_at.is_(None)]
        if q:
            kwhere.append(ApiKey.name.ilike(f"%{q}%"))
        keys = (await db.execute(select(ApiKey).where(*kwhere))).scalars().all()
        for key in keys:
            scopes = list(key.scopes or [])
            rows.append(
                InstallationRow(
                    kind="api_key",
                    id=str(key.id),
                    name=key.name,
                    publisher="tenant",
                    version=None,
                    granted_scopes=scopes,
                    sensitive_scopes=_sensitive(scopes),
                    status="active",
                    last_activity_at=key.last_used_at,
                )
            )
    return rows


async def authorizations(
    ctx: RequestContext, db: AsyncSession, *, installation_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    where = [AppUserAuthorization.tenant_id == ctx.tenant_id, AppUserAuthorization.revoked_at.is_(None)]
    if installation_id:
        where.append(AppUserAuthorization.installation_id == installation_id)
    if user_id:
        where.append(AppUserAuthorization.user_id == user_id)
    auths = (await db.execute(select(AppUserAuthorization).where(*where))).scalars().all()
    out: list[dict[str, Any]] = []
    for auth in auths:
        last = (
            await db.execute(
                select(func.max(OAuthToken.last_used_at)).where(OAuthToken.user_authorization_id == auth.id)
            )
        ).scalar_one_or_none()
        out.append(
            {
                "id": str(auth.id),
                "installation_id": str(auth.installation_id),
                "user_id": str(auth.user_id),
                "scopes": list(auth.scopes or []),
                "granted_at": auth.granted_at,
                "last_used_at": last,
            }
        )
    return out


async def violations(ctx: RequestContext, db: AsyncSession, **_: Any) -> list[ViolationRow]:
    from app.services.ecosystem_governance import violations as violations_mod

    return await violations_mod.list_violations(ctx, db)


__all__ = ["authorizations", "installations", "summary", "violations"]
