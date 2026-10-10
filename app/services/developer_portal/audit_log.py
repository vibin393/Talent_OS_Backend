# ruff: noqa: E501
"""The partner org audit log (Epic 74, spec A5): append-only rows for every credential and workspace action. Details
carry names, ids and counts only -- never secrets, codes or tokens (a key filter drops anything that looks like one)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_context
from app.core.database import platform_session
from app.models.developer import DeveloperAccount, PartnerOrgAuditLog
from app.schemas.developer import AuditRowOut

_FORBIDDEN_KEYS = frozenset({"secret", "token", "code", "recovery_code", "password", "otp", "totp"})

LABELS = {
    "account.signed_in": "Signed in",
    "account.sign_in_failed": "Sign-in failed",
    "account.mfa_enrolled": "Turned on two-step verification",
    "account.password_changed": "Changed the password",
    "org.created": "Created the organisation",
    "org.renamed": "Renamed the organisation",
    "org.member_invited": "Invited a member",
    "org.member_joined": "Joined the organisation",
    "org.member_role_changed": "Changed a member's role",
    "org.member_removed": "Removed a member",
    "org.verification_submitted": "Applied for Verified Partner status",
    "org.verification_decided": "Verified Partner decision",
    "app.created": "Created an app",
    "app.updated": "Updated an app",
    "app.secret_rotated": "Rotated the client secret",
    "app.secret_revoked": "Revoked a client secret",
    "app.retired": "Retired an app",
    "listing.saved": "Saved a listing draft",
    "listing.submitted": "Submitted a version for review",
    "listing.withdrawn": "Withdrew a version",
    "listing.status_changed": "Submission status changed",
    "sandbox.created": "Created a sandbox",
    "sandbox.opened": "Opened a sandbox",
    "sandbox.reset": "Reset a sandbox",
    "sandbox.deleted": "Deleted a sandbox",
    "sandbox.app_installed": "Installed an app in a sandbox",
    "conformance.started": "Started a conformance run",
    "conformance.finished": "Finished a conformance run",
    "playground.token_issued": "Got a playground token",
}


def _clean(details: dict[str, Any]) -> dict[str, Any]:
    def bad(key: str) -> bool:
        k = key.lower()
        return k in _FORBIDDEN_KEYS or k.endswith(("_secret", "_token", "_password"))

    return {k: v for k, v in details.items() if not bad(k)}


def add(
    db: AsyncSession,
    action: str,
    *,
    partner_org_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    operator_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    **details: Any,
) -> None:
    db.add(
        PartnerOrgAuditLog(
            id=uuid.uuid4(), partner_org_id=partner_org_id, account_id=account_id, operator_id=operator_id, action=action[:60],
            target_type=target_type, target_id=target_id, details=_clean({k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in details.items()}),
            ip=get_context().ip,
        )
    )


async def record(action: str, **kw: Any) -> None:
    async with platform_session() as db:
        add(db, action, **kw)


async def rows(org_id: uuid.UUID, *, action: str | None = None, account_id: uuid.UUID | None = None, limit: int = 200) -> list[AuditRowOut]:
    async with platform_session() as db:
        stmt = (
            select(PartnerOrgAuditLog, DeveloperAccount.name)
            .outerjoin(DeveloperAccount, DeveloperAccount.id == PartnerOrgAuditLog.account_id)
            .where(PartnerOrgAuditLog.partner_org_id == org_id)
        )
        if action:
            stmt = stmt.where(PartnerOrgAuditLog.action == action)
        if account_id:
            stmt = stmt.where(PartnerOrgAuditLog.account_id == account_id)
        result = await db.execute(stmt.order_by(PartnerOrgAuditLog.at.desc()).limit(min(limit, 1000)))
        return [
            AuditRowOut(
                id=r.id, at=r.at, account_id=r.account_id, actor_name=name or ("Talent OS reviewer" if r.operator_id else None),
                action=r.action, target_type=r.target_type, target_id=r.target_id, details=dict(r.details or {}),
            )
            for r, name in result.all()
        ]


__all__ = ["LABELS", "add", "record", "rows"]
