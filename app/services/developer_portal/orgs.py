# ruff: noqa: E501
"""Partner organisations (Epic 74, story 74.1, spec 0.1 / 0.3): create (owner membership; at most 3 owned per account),
members and roles (the last owner can never leave or be demoted -> 409 ``last_owner``), invitations (one-time link,
7 days, same email, at most 25 open per org) and the org audit log."""

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.developer import DeveloperContext
from app.core.errors import ConflictError, Gone, NotFound
from app.core.security import hash_token
from app.core.settings import get_settings
from app.models.developer import DeveloperAccount, PartnerOrgInvitation, PartnerOrgMember
from app.models.oauth import PartnerOrg
from app.schemas.developer import (
    AuditRowOut,
    InvitationOut,
    InviteIn,
    MemberOut,
    OrgCreate,
    OrgOut,
    OrgUpdate,
)
from app.services.developer_portal import audit_log, notifier
from app.services.developer_portal.accounts import org_out
from app.services.developer_portal.common import now, require_role

MAX_OPEN_INVITATIONS = 25


async def create_org(dctx: DeveloperContext, data: OrgCreate) -> OrgOut:
    s = get_settings()
    async with platform_session() as db:
        owned = (
            await db.execute(
                select(func.count()).select_from(PartnerOrgMember).where(
                    PartnerOrgMember.account_id == dctx.developer_id, PartnerOrgMember.role == "owner", PartnerOrgMember.removed_at.is_(None)
                )
            )
        ).scalar_one()
        if owned >= s.partner_orgs_per_account:
            raise ConflictError(f"You can own at most {s.partner_orgs_per_account} organisations", code="org_limit")
        if (await db.execute(select(PartnerOrg.id).where(PartnerOrg.slug == data.slug))).first():
            raise ConflictError("This URL name is taken", code="slug_taken")
        org = PartnerOrg(id=uuid.uuid4(), name=data.name.strip(), slug=data.slug, status="active", verification_status="unverified",
                         created_by_account_id=dctx.developer_id)  # fmt: skip
        db.add(org)
        await db.flush()
        db.add(PartnerOrgMember(id=uuid.uuid4(), partner_org_id=org.id, account_id=dctx.developer_id, role="owner", joined_at=now()))
        audit_log.add(db, "org.created", partner_org_id=org.id, account_id=dctx.developer_id, target_type="partner_org", target_id=org.id, name=org.name)
        await db.flush()
        await db.refresh(org)
        return org_out(org, "owner")


async def get_org(dctx: DeveloperContext, org_id: uuid.UUID) -> OrgOut:
    require_role(dctx, "viewer", org_id)
    async with platform_session() as db:
        org = await db.get(PartnerOrg, org_id)
        if org is None:
            raise NotFound("Organisation not found")
        return org_out(org, dctx.role)


async def update_org(dctx: DeveloperContext, org_id: uuid.UUID, data: OrgUpdate) -> OrgOut:
    require_role(dctx, "owner", org_id)
    async with platform_session() as db:
        org = await db.get(PartnerOrg, org_id)
        if org is None:
            raise NotFound("Organisation not found")
        org.name = data.name.strip()
        audit_log.add(db, "org.renamed", partner_org_id=org_id, account_id=dctx.developer_id, name=org.name)
        await db.flush()
        return org_out(org, dctx.role)


async def members(dctx: DeveloperContext, org_id: uuid.UUID) -> list[MemberOut]:
    require_role(dctx, "viewer", org_id)
    async with platform_session() as db:
        rows = await db.execute(
            select(PartnerOrgMember, DeveloperAccount)
            .join(DeveloperAccount, DeveloperAccount.id == PartnerOrgMember.account_id)
            .where(PartnerOrgMember.partner_org_id == org_id, PartnerOrgMember.removed_at.is_(None))
            .order_by(PartnerOrgMember.joined_at)
        )
        return [MemberOut(account_id=a.id, name=a.name, email=str(a.email), role=m.role, joined_at=m.joined_at) for m, a in rows.all()]  # type: ignore[arg-type]


async def _live_member(db: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> PartnerOrgMember:
    row = (
        await db.execute(
            select(PartnerOrgMember).where(
                PartnerOrgMember.partner_org_id == org_id, PartnerOrgMember.account_id == account_id, PartnerOrgMember.removed_at.is_(None)
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Member not found")
    return row


async def _other_owners(db: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(PartnerOrgMember).where(
                    PartnerOrgMember.partner_org_id == org_id, PartnerOrgMember.role == "owner",
                    PartnerOrgMember.removed_at.is_(None), PartnerOrgMember.account_id != account_id,
                )
            )
        ).scalar_one()
    )  # fmt: skip


async def set_member_role(dctx: DeveloperContext, org_id: uuid.UUID, account_id: uuid.UUID, role: str) -> MemberOut:
    require_role(dctx, "owner", org_id)
    async with platform_session() as db:
        member = await _live_member(db, org_id, account_id)
        if member.role == "owner" and role != "owner" and await _other_owners(db, org_id, account_id) == 0:
            raise ConflictError("An organisation needs at least one owner", code="last_owner")
        before, member.role = member.role, role
        audit_log.add(db, "org.member_role_changed", partner_org_id=org_id, account_id=dctx.developer_id, target_type="developer_account",
                      target_id=account_id, before=before, after=role)  # fmt: skip
        account = await db.get(DeveloperAccount, account_id)
        assert account is not None
        return MemberOut(account_id=account.id, name=account.name, email=str(account.email), role=role, joined_at=member.joined_at)  # type: ignore[arg-type]


async def remove_member(dctx: DeveloperContext, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    require_role(dctx, "owner" if account_id != dctx.developer_id else "viewer", org_id)  # anyone may leave
    async with platform_session() as db:
        member = await _live_member(db, org_id, account_id)
        if member.role == "owner" and await _other_owners(db, org_id, account_id) == 0:
            raise ConflictError("An organisation needs at least one owner", code="last_owner")
        member.removed_at = now()
        audit_log.add(db, "org.member_removed", partner_org_id=org_id, account_id=dctx.developer_id, target_type="developer_account", target_id=account_id)


async def invite(dctx: DeveloperContext, org_id: uuid.UUID, data: InviteIn) -> InvitationOut:
    require_role(dctx, "owner", org_id)
    s = get_settings()
    email = str(data.email).strip().lower()
    async with platform_session() as db:
        org = await db.get(PartnerOrg, org_id)
        inviter = await db.get(DeveloperAccount, dctx.developer_id)
        if org is None or inviter is None:
            raise NotFound("Organisation not found")
        open_count = (
            await db.execute(
                select(func.count()).select_from(PartnerOrgInvitation).where(
                    PartnerOrgInvitation.partner_org_id == org_id, PartnerOrgInvitation.accepted_at.is_(None),
                    PartnerOrgInvitation.revoked_at.is_(None), PartnerOrgInvitation.expires_at > now(),
                )
            )
        ).scalar_one()  # fmt: skip
        if open_count >= MAX_OPEN_INVITATIONS:
            raise ConflictError(f"At most {MAX_OPEN_INVITATIONS} open invitations", code="invitation_limit")
        already = (
            await db.execute(
                select(PartnerOrgMember.id).join(DeveloperAccount, DeveloperAccount.id == PartnerOrgMember.account_id).where(
                    PartnerOrgMember.partner_org_id == org_id, PartnerOrgMember.removed_at.is_(None), DeveloperAccount.email == email
                )
            )
        ).first()  # fmt: skip
        if already:
            raise ConflictError("This person is already a member", code="already_member")
        token = secrets.token_urlsafe(32)
        row = PartnerOrgInvitation(id=uuid.uuid4(), partner_org_id=org_id, email=email, role=data.role, token_hash=hash_token(token),
                                   invited_by=dctx.developer_id, expires_at=now() + timedelta(days=s.developer_invitation_days))  # fmt: skip
        db.add(row)
        audit_log.add(db, "org.member_invited", partner_org_id=org_id, account_id=dctx.developer_id, target_type="invitation", target_id=row.id, role=data.role)
        await db.flush()
        out = InvitationOut(id=row.id, email=email, role=data.role, expires_at=row.expires_at)
        inviter_name, org_name = inviter.name, org.name
    await notifier.invitation(email, inviter_name, org_name, token)
    return out


async def accept_invitation(dctx: DeveloperContext, token: str) -> OrgOut:
    async with platform_session() as db:
        inv = (
            await db.execute(select(PartnerOrgInvitation).where(PartnerOrgInvitation.token_hash == hash_token(token)).with_for_update())
        ).scalar_one_or_none()
        if inv is None or inv.accepted_at is not None or inv.revoked_at is not None or inv.expires_at <= now():
            raise Gone("This invitation is invalid or has expired", code="invitation_expired")
        account = await db.get(DeveloperAccount, dctx.developer_id)
        if account is None or account.email_verified_at is None or str(account.email).lower() != str(inv.email).lower():
            raise ConflictError("Sign in with the email address the invitation was sent to", code="invitation_email_mismatch")
        inv.accepted_at = now()
        try:
            async with db.begin_nested():
                db.add(PartnerOrgMember(id=uuid.uuid4(), partner_org_id=inv.partner_org_id, account_id=account.id, role=inv.role,
                                        invited_by=inv.invited_by, joined_at=now()))  # fmt: skip
        except IntegrityError as exc:
            raise ConflictError("You are already a member", code="already_member") from exc
        audit_log.add(db, "org.member_joined", partner_org_id=inv.partner_org_id, account_id=account.id, role=inv.role)
        org = await db.get(PartnerOrg, inv.partner_org_id)
        assert org is not None
        return org_out(org, inv.role)


async def org_audit(dctx: DeveloperContext, org_id: uuid.UUID, *, action: str | None = None, account_id: uuid.UUID | None = None) -> list[AuditRowOut]:
    require_role(dctx, "viewer", org_id)
    return await audit_log.rows(org_id, action=action, account_id=account_id)


async def expire_invitations() -> int:
    """Daily: open invitations past ``expires_at`` are marked revoked (their links already fail)."""
    from sqlalchemy import update

    async with platform_session() as db:
        result = await db.execute(
            update(PartnerOrgInvitation)
            .where(PartnerOrgInvitation.accepted_at.is_(None), PartnerOrgInvitation.revoked_at.is_(None), PartnerOrgInvitation.expires_at < now())
            .values(revoked_at=now())
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = [
    "accept_invitation",
    "create_org",
    "expire_invitations",
    "get_org",
    "invite",
    "members",
    "org_audit",
    "remove_member",
    "set_member_role",
    "update_org",
]
