# ruff: file-ignore[line-too-long]
"""Developer account erasure (Epic 74, spec A3; platform subject, Known gap 3). Self-service "Delete account" and the
platform operator action "Erase" both run ``erase_account``: anonymise the account, delete its sessions, email tokens
and recovery codes, remove org memberships and null the IP of its org audit rows."""

import uuid

from sqlalchemy import delete, update

from app.core.database import platform_session
from app.models.developer import (
    DeveloperAccount,
    DeveloperEmailToken,
    DeveloperRecoveryCode,
    DeveloperSession,
    PartnerOrgAuditLog,
    PartnerOrgMember,
)
from app.services.developer_portal.common import now


async def erase_account(account_id: uuid.UUID, *, operator_id: uuid.UUID | None = None) -> bool:
    async with platform_session() as db:
        account = await db.get(DeveloperAccount, account_id)
        if account is None:
            return False
        account.email = f"erased+{account.id.hex}@example.invalid"
        account.name = "Erased developer"
        account.password_hash = "!"
        account.mfa_secret_enc = None
        account.mfa_enabled_at = None
        account.status = "closed"
        account.closed_at = now()
        for model in (DeveloperSession, DeveloperEmailToken, DeveloperRecoveryCode):
            await db.execute(delete(model).where(model.account_id == account_id))
        await db.execute(update(PartnerOrgMember).where(PartnerOrgMember.account_id == account_id, PartnerOrgMember.removed_at.is_(None)).values(removed_at=now()))
        await db.execute(update(PartnerOrgAuditLog).where(PartnerOrgAuditLog.account_id == account_id).values(ip=None))
        db.add(PartnerOrgAuditLog(id=uuid.uuid4(), account_id=account_id, operator_id=operator_id, action="account.erased", details={}))
    return True


__all__ = ["erase_account"]
