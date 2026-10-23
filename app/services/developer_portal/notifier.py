# ruff: file-ignore[line-too-long]
"""Partner emails (Epic 74, spec A5). Developer accounts are not tenant users, so these are templated platform emails
through ``core.mailer`` (outbox in tests), never tenant notification types. ``partner_submission_status`` is fed by
Epic 69's ``register_partner_notifier`` hook (registered in ``hooks.py``)."""

import uuid

from loguru import logger
from sqlalchemy import select

from app.core.database import platform_session
from app.core.mailer import EmailMessage, send_email
from app.core.settings import get_settings
from app.models.developer import DeveloperAccount, PartnerOrgMember
from app.services.developer_portal import audit_log

STATUS_LABELS = {
    "draft": "Draft", "submitted": "Submitted", "automated_checks": "Automated checks", "in_review": "In review",
    "changes_requested": "Changes requested", "approved": "Approved", "published": "Published", "rejected": "Not approved",
    "suspended": "Suspended", "delisted": "Delisted", "superseded": "Superseded",
}  # fmt: skip
DECISION_LABELS = {"approve": "Approved", "request_changes": "Changes requested", "reject": "Not approved"}


def site(path: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}{path}"


async def send(kind: str, to: str, subject: str, text: str) -> None:
    try:
        await send_email(EmailMessage(to=to, subject=subject, text=text, kind=kind))
    except Exception:
        logger.exception("Developer email {} could not be sent", kind)


async def org_recipients(org_id: uuid.UUID, roles: tuple[str, ...]) -> list[str]:
    async with platform_session() as db:
        rows = await db.execute(
            select(DeveloperAccount.email)
            .join(PartnerOrgMember, PartnerOrgMember.account_id == DeveloperAccount.id)
            .where(
                PartnerOrgMember.partner_org_id == org_id,
                PartnerOrgMember.removed_at.is_(None),
                PartnerOrgMember.role.in_(roles),
                DeveloperAccount.status == "active",
            )
        )
        return [str(e) for e in rows.scalars()]


async def verify_email(to: str, token: str) -> None:
    await send("developer_verify_email", to, "Verify your Talent OS developer account",
               f"Confirm your email address to finish creating your developer account:\n\n{site('/developers/verify-email')}#t={token}\n\nThe link works for 24 hours.")  # fmt: skip


async def account_exists(to: str) -> None:
    await send("developer_verify_email", to, "Verify your Talent OS developer account",
               f"Someone tried to create a developer account with this address, but you already have one.\n\nSign in: {site('/developers/sign-in')}")  # fmt: skip


async def password_reset(to: str, token: str) -> None:
    await send("developer_password_reset", to, "Reset your Talent OS developer password",
               f"Choose a new password:\n\n{site('/developers/reset-password')}#t={token}\n\nThe link works for 1 hour. If you didn't ask for this, ignore this email.")  # fmt: skip


async def invitation(to: str, inviter: str, org: str, token: str) -> None:
    await send("developer_invitation", to, f"{inviter} invited you to {org} on Talent OS",
               f"{inviter} invited you to join {org} on Talent OS for Developers.\n\nAccept: {site('/developers/invitations/accept')}#t={token}\n\nThe invitation expires in 7 days.")  # fmt: skip


async def verification_decided(org_id: uuid.UUID, org_name: str, decision: str, message: str | None) -> None:
    label = DECISION_LABELS.get(decision, decision)
    body = f"Your Verified Partner application for {org_name}: {label}."
    if message:
        body += f"\n\nMessage from the reviewer:\n{message}"
    for to in await org_recipients(org_id, ("owner",)):
        await send("partner_verification_decided", to, f"Your Verified Partner application: {label}", body)


async def verification_expiring(org_id: uuid.UUID, org_name: str, expires: str) -> None:
    for to in await org_recipients(org_id, ("owner",)):
        await send("partner_verification_expiring", to, f"Your Verified Partner status expires on {expires}",
                   f"The Verified Partner status of {org_name} expires on {expires}. Renew it from the Organisation page.")  # fmt: skip


async def sandbox_status(to: str, name: str, *, ready: bool) -> None:
    if ready:
        await send("sandbox_ready", to, f"Your sandbox {name} is ready", f"Your sandbox {name} is ready. Open it from {site('/developers/workspace/sandboxes')}.")
    else:
        await send("sandbox_failed", to, f"Your sandbox {name} couldn't be created", "Try again in a few minutes. If it keeps failing, contact support.")


async def sandbox_inactive_warning(to: str, name: str) -> None:
    await send("sandbox_inactive_warning", to, f"Your sandbox {name} will be deleted in 14 days",
               f"Your sandbox {name} has not been used for a while and will be deleted in 14 days. Open it to keep it.")  # fmt: skip


async def new_sign_in(to: str, ip: str | None) -> None:
    await send("developer_new_sign_in", to, "New sign-in to your developer account",
               f"Your developer account was signed in to from a new device or network ({ip or 'unknown address'}). If this wasn't you, reset your password.")  # fmt: skip


async def on_submission_status(partner_org_id: uuid.UUID, version_id: uuid.UUID, status: str) -> None:
    """Epic 69 ``register_partner_notifier`` hook: email owners + developers and write an org audit row."""
    label = STATUS_LABELS.get(status, status)
    from app.models.marketplace import MarketplaceListing, MarketplaceListingVersion

    async with platform_session() as db:
        row = (
            await db.execute(
                select(MarketplaceListing.name, MarketplaceListingVersion.version_no)
                .join(MarketplaceListingVersion, MarketplaceListingVersion.listing_id == MarketplaceListing.id)
                .where(MarketplaceListingVersion.id == version_id)
            )
        ).first()
        name, number = (row[0], row[1]) if row else ("Your app", 0)
        audit_log.add(db, "listing.status_changed", partner_org_id=partner_org_id, target_type="listing_version", target_id=version_id, status=status)
    for to in await org_recipients(partner_org_id, ("owner", "developer")):
        await send("partner_submission_status", to, f"{name} version {number}: {label}",
                   f"{name} version {number} is now: {label}.\n\nDetails: {site('/developers/workspace/submissions')}\n\nUnsubscribe from submission emails in your account settings.")  # fmt: skip


__all__ = [
    "DECISION_LABELS",
    "STATUS_LABELS",
    "account_exists",
    "invitation",
    "new_sign_in",
    "on_submission_status",
    "org_recipients",
    "password_reset",
    "sandbox_inactive_warning",
    "sandbox_status",
    "verification_decided",
    "verification_expiring",
    "verify_email",
]
