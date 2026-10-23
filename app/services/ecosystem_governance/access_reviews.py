# ruff: file-ignore[line-too-long]
"""Quarterly access reviews (Epic 79 story 79.2, FR-406).

A review holds one item per active Installation grant, active delegated authorisation and active API key. ``stale`` is
``last_used_at IS NULL OR last_used_at < now - stale_after_days``. Reviewers confirm / reduce / revoke; with
``auto_revoke_stale`` the system auto-revokes stale items undecided for 14 days. The tables use ``AuditedMixin`` so every
decision and completion is audited through the ORM.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.models.api_keys import ApiKey
from app.models.ecosystem_governance import AccessReview, AccessReviewItem, AccessReviewSettings
from app.models.oauth import AppInstallation, AppUserAuthorization, OAuthToken

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

AUTO_REVOKE_AFTER_DAYS = 14
ACTIVE_INSTALL_STATUSES = ("active", "degraded")


async def get_settings(db: AsyncSession, tenant_id: uuid.UUID) -> AccessReviewSettings:
    row = (
        await db.execute(select(AccessReviewSettings).where(AccessReviewSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = AccessReviewSettings(tenant_id=tenant_id)
        db.add(row)
        await db.flush()
    return row


async def update_settings(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cadence_months: int,
    stale_after_days: int,
    auto_revoke_stale: bool,
    reviewer_role_key: str | None,
    updated_by: uuid.UUID | None,
) -> AccessReviewSettings:
    row = await get_settings(db, tenant_id)
    row.cadence_months = cadence_months
    row.stale_after_days = stale_after_days
    row.auto_revoke_stale = auto_revoke_stale
    row.reviewer_role_key = reviewer_role_key
    row.updated_by = updated_by
    await db.flush()
    return row


async def _install_last_used(db: AsyncSession, installation_id: uuid.UUID) -> dt.datetime | None:
    return (
        await db.execute(
            select(func.max(OAuthToken.last_used_at)).where(OAuthToken.installation_id == installation_id)
        )
    ).scalar_one_or_none()


async def _auth_last_used(db: AsyncSession, auth_id: uuid.UUID) -> dt.datetime | None:
    return (
        await db.execute(
            select(func.max(OAuthToken.last_used_at)).where(OAuthToken.user_authorization_id == auth_id)
        )
    ).scalar_one_or_none()


async def _collect(db: AsyncSession, tenant_id: uuid.UUID, cutoff: dt.datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    installs = (
        await db.execute(
            select(AppInstallation).where(
                AppInstallation.tenant_id == tenant_id,
                AppInstallation.status.in_(ACTIVE_INSTALL_STATUSES),
            )
        )
    ).scalars().all()
    for inst in installs:
        last = await _install_last_used(db, inst.id)
        items.append(
            {
                "kind": "installation_grant",
                "subject_id": inst.id,
                "snapshot": {
                    "name": inst.display_name,
                    "scopes": list(inst.granted_scopes or []),
                    "granted_at": inst.granted_at.isoformat() if inst.granted_at else None,
                    "last_used_at": last.isoformat() if last else None,
                },
                "stale": last is None or last < cutoff,
            }
        )
    auths = (
        await db.execute(
            select(AppUserAuthorization).where(
                AppUserAuthorization.tenant_id == tenant_id,
                AppUserAuthorization.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    for auth in auths:
        last = await _auth_last_used(db, auth.id)
        items.append(
            {
                "kind": "user_authorization",
                "subject_id": auth.id,
                "snapshot": {
                    "scopes": list(auth.scopes or []),
                    "user_id": str(auth.user_id),
                    "last_used_at": last.isoformat() if last else None,
                },
                "stale": last is None or last < cutoff,
            }
        )
    keys = (
        await db.execute(
            select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
        )
    ).scalars().all()
    for key in keys:
        last = key.last_used_at
        items.append(
            {
                "kind": "api_key",
                "subject_id": key.id,
                "snapshot": {
                    "name": key.name,
                    "scopes": list(key.scopes or []),
                    "last_used_at": last.isoformat() if last else None,
                },
                "stale": last is None or last < cutoff,
            }
        )
    return items


async def open_review(db: AsyncSession, tenant_id: uuid.UUID, *, today: dt.date | None = None) -> AccessReview:
    """Open a review covering every active grant / authorisation / key. Idempotent while one is already open."""
    existing = (
        await db.execute(
            select(AccessReview).where(AccessReview.tenant_id == tenant_id, AccessReview.status == "open")
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    settings = await get_settings(db, tenant_id)
    today = today or dt.date.today()
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.stale_after_days)
    period_start = today.replace(day=1)
    period_end = period_start + dt.timedelta(days=30 * max(1, settings.cadence_months))
    review = AccessReview(
        tenant_id=tenant_id, period_start=period_start, period_end=period_end, status="open"
    )
    db.add(review)
    await db.flush()
    collected = await _collect(db, tenant_id, cutoff)
    for item in collected:
        db.add(
            AccessReviewItem(
                tenant_id=tenant_id,
                review_id=review.id,
                kind=item["kind"],
                subject_id=item["subject_id"],
                snapshot=item["snapshot"],
                stale=item["stale"],
                decision="pending",
            )
        )
    review.item_count = len(collected)
    await db.flush()
    return review


async def cadence_due(db: AsyncSession, tenant_id: uuid.UUID, *, today: dt.date | None = None) -> bool:
    today = today or dt.date.today()
    settings = await get_settings(db, tenant_id)
    last = (
        await db.execute(
            select(func.max(AccessReview.opened_at)).where(AccessReview.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if last is None:
        return True
    return last.date() <= today - dt.timedelta(days=30 * max(1, settings.cadence_months))


async def decide_item(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    decision: str,
    decided_by: uuid.UUID | None,
    note: str | None,
) -> AccessReviewItem:
    item = (
        await db.execute(
            select(AccessReviewItem).where(
                AccessReviewItem.tenant_id == tenant_id, AccessReviewItem.id == item_id
            )
        )
    ).scalar_one()
    item.decision = decision
    item.decided_by = decided_by
    item.decided_at = dt.datetime.now(dt.UTC)
    item.note = note
    await db.flush()
    await _recount(db, tenant_id, item.review_id)
    return item


async def _recount(db: AsyncSession, tenant_id: uuid.UUID, review_id: uuid.UUID) -> None:
    decided = (
        await db.execute(
            select(func.count())
            .select_from(AccessReviewItem)
            .where(
                AccessReviewItem.tenant_id == tenant_id,
                AccessReviewItem.review_id == review_id,
                AccessReviewItem.decision != "pending",
            )
        )
    ).scalar_one()
    review = (
        await db.execute(select(AccessReview).where(AccessReview.id == review_id))
    ).scalar_one()
    review.decided_count = int(decided)
    await db.flush()


async def complete_review(
    db: AsyncSession, tenant_id: uuid.UUID, review_id: uuid.UUID, *, completed_by: uuid.UUID | None
) -> AccessReview:
    review = (
        await db.execute(
            select(AccessReview).where(AccessReview.tenant_id == tenant_id, AccessReview.id == review_id)
        )
    ).scalar_one()
    review.status = "completed"
    review.completed_at = dt.datetime.now(dt.UTC)
    review.completed_by = completed_by
    await db.flush()
    return review


async def auto_revoke_due(db: AsyncSession, tenant_id: uuid.UUID) -> list[AccessReviewItem]:
    """Auto-revoke stale, undecided items older than 14 days when the tenant opted in. Never touches Tenant Admin
    keys or the kill switch. Returns the items marked ``auto_revoked`` (callers fan out the actual revocations)."""
    settings = await get_settings(db, tenant_id)
    if not settings.auto_revoke_stale:
        return []
    threshold = dt.datetime.now(dt.UTC) - dt.timedelta(days=AUTO_REVOKE_AFTER_DAYS)
    rows = (
        await db.execute(
            select(AccessReviewItem)
            .join(AccessReview, AccessReview.id == AccessReviewItem.review_id)
            .where(
                AccessReviewItem.tenant_id == tenant_id,
                AccessReviewItem.stale.is_(True),
                AccessReviewItem.decision == "pending",
                AccessReview.status == "open",
                AccessReview.opened_at < threshold,
            )
        )
    ).scalars().all()
    for item in rows:
        item.decision = "auto_revoked"
        item.decided_at = dt.datetime.now(dt.UTC)
        item.note = "Auto-revoked: stale and undecided for 14 days."
    if rows:
        await db.flush()
    return list(rows)


async def list_reviews(db: AsyncSession, tenant_id: uuid.UUID) -> list[AccessReview]:
    return list(
        (
            await db.execute(
                select(AccessReview)
                .where(AccessReview.tenant_id == tenant_id)
                .order_by(AccessReview.opened_at.desc())
            )
        ).scalars().all()
    )


async def list_items(
    db: AsyncSession, tenant_id: uuid.UUID, review_id: uuid.UUID, *, stale_only: bool = False, kind: str | None = None
) -> list[AccessReviewItem]:
    where = [AccessReviewItem.tenant_id == tenant_id, AccessReviewItem.review_id == review_id]
    if stale_only:
        where.append(AccessReviewItem.stale.is_(True))
    if kind:
        where.append(AccessReviewItem.kind == kind)
    return list((await db.execute(select(AccessReviewItem).where(*where))).scalars().all())


__all__ = [
    "AUTO_REVOKE_AFTER_DAYS",
    "auto_revoke_due",
    "cadence_due",
    "complete_review",
    "decide_item",
    "get_settings",
    "list_items",
    "list_reviews",
    "open_review",
    "update_settings",
]
