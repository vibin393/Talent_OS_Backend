"""Maintenance jobs (Epic 12 A10; deferred-work items). Each deletes in batches of 5 000 rows with
``FOR UPDATE SKIP LOCKED`` and is idempotent.

| Job                            | Schedule | Deletes                                                           |
|--------------------------------|----------|-------------------------------------------------------------------|
| ``purge_sessions``             | hourly   | expired sessions, or revoked > 7 days ago (support sessions too)   |
| ``purge_password_resets``      | hourly   | expired, or used > 24 h ago                                        |
| ``purge_invitations``          | daily    | expired > 30 days ago                                              |
| ``purge_secure_links``         | daily    | lookup rows 30 days after expiry/consumption/revocation; links 180 d |
| ``purge_api_key_request_log``  | daily    | log rows older than 90 days                                        |

``sessions`` and ``secure_link_lookups`` are platform tables (one pass); the rest are tenant-owned (RLS), so
they are purged tenant by tenant.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery import celery_app
from app.core.database import platform_session, tenant_session
from app.core.secure_links import SecureLink, SecureLinkLookup
from app.models.api_keys import ApiKeyRequestLog
from app.models.identity import Invitation, PasswordReset
from app.models.identity import Session as AuthSession
from app.tasks.ops_runtime import for_each_tenant, run

QUEUE = "maintenance"  # Epic 13 (AD-37): the queue of every task in this module

BATCH = 5000
ALL_TENANTS = ("active", "suspended", "offboarding")


def _now() -> datetime:
    return datetime.now(UTC)


async def _purge(db: AsyncSession, model: Any, condition: Any, pk: Any = None) -> int:
    """Delete rows matching ``condition`` in batches (``SKIP LOCKED`` so concurrent runs never block)."""
    pk = pk if pk is not None else model.id
    total = 0
    while True:
        ids = select(pk).where(condition).limit(BATCH).with_for_update(skip_locked=True).scalar_subquery()
        result = await db.execute(delete(model).where(pk.in_(ids)).execution_options(synchronize_session=False))
        count = int(result.rowcount or 0)  # type: ignore[attr-defined]
        total += count
        await db.commit()
        if count < BATCH:
            return total


async def purge_sessions_job(now: datetime | None = None) -> int:
    now = now or _now()
    cond = or_(AuthSession.expires_at < now, AuthSession.revoked_at < now - timedelta(days=7))
    async with platform_session() as db:
        return await _purge(db, AuthSession, cond)


async def purge_password_resets_job(now: datetime | None = None, only: str | None = None) -> dict[str, Any]:
    now = now or _now()
    cond = or_(PasswordReset.expires_at < now, PasswordReset.used_at < now - timedelta(hours=24))

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            return await _purge(db, PasswordReset, cond)

    return await for_each_tenant(one, ALL_TENANTS, only=only)


async def purge_invitations_job(now: datetime | None = None, only: str | None = None) -> dict[str, Any]:
    now = now or _now()
    cond = Invitation.expires_at < now - timedelta(days=30)

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            return await _purge(db, Invitation, cond)

    return await for_each_tenant(one, ALL_TENANTS, only=only)


async def purge_secure_links_job(now: datetime | None = None, only: str | None = None) -> dict[str, Any]:
    now = now or _now()
    closed_before = now - timedelta(days=30)
    closed = or_(
        SecureLink.expires_at < closed_before,
        SecureLink.revoked_at < closed_before,
        and_(SecureLink.consumed_at < closed_before, SecureLink.use_count >= SecureLink.max_uses),
    )

    async def one(tid: uuid.UUID) -> dict[str, int]:
        async with tenant_session(tid) as db:
            stale = list((await db.execute(select(SecureLink.id).where(closed))).scalars())
            lookups = 0
            for start in range(0, len(stale), BATCH):
                chunk = stale[start : start + BATCH]
                res = await db.execute(delete(SecureLinkLookup).where(SecureLinkLookup.link_id.in_(chunk)))
                lookups += int(res.rowcount or 0)  # type: ignore[attr-defined]
            links = await _purge(db, SecureLink, SecureLink.created_at < now - timedelta(days=180))
            return {"lookups": lookups, "links": links}

    return await for_each_tenant(one, ALL_TENANTS, only=only)


async def purge_api_key_request_log_job(now: datetime | None = None, only: str | None = None) -> dict[str, Any]:
    now = now or _now()
    cond = ApiKeyRequestLog.at < now - timedelta(days=90)

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            return await _purge(db, ApiKeyRequestLog, cond)

    return await for_each_tenant(one, ALL_TENANTS, only=only)


@celery_app.task(name="maintenance.purge_sessions")  # type: ignore[untyped-decorator,unused-ignore]
def purge_sessions() -> int:
    return run(purge_sessions_job)  # type: ignore[no-any-return]


@celery_app.task(name="maintenance.purge_password_resets")  # type: ignore[untyped-decorator,unused-ignore]
def purge_password_resets(tenant_id: str | None = None) -> dict[str, Any]:
    return run(lambda: purge_password_resets_job(only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="maintenance.purge_invitations")  # type: ignore[untyped-decorator,unused-ignore]
def purge_invitations(tenant_id: str | None = None) -> dict[str, Any]:
    return run(lambda: purge_invitations_job(only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="maintenance.purge_secure_links")  # type: ignore[untyped-decorator,unused-ignore]
def purge_secure_links(tenant_id: str | None = None) -> dict[str, Any]:
    return run(lambda: purge_secure_links_job(only=tenant_id))  # type: ignore[no-any-return]


@celery_app.task(name="maintenance.purge_api_key_request_log")  # type: ignore[untyped-decorator,unused-ignore]
def purge_api_key_request_log(tenant_id: str | None = None) -> dict[str, Any]:
    return run(lambda: purge_api_key_request_log_job(only=tenant_id))  # type: ignore[no-any-return]


async def purge_idempotency_keys_job(now: datetime | None = None, only: str | None = None) -> dict[str, Any]:
    """Epic 13 (AD-23): idempotency keys expire after 24 h."""
    from app.core.idempotency import purge_expired

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            return await purge_expired(db, now or _now())

    return await for_each_tenant(one, ALL_TENANTS, only=only)


@celery_app.task(name="maintenance.purge_idempotency_keys")  # type: ignore[untyped-decorator,unused-ignore]
def purge_idempotency_keys(tenant_id: str | None = None) -> dict[str, Any]:
    return run(lambda: purge_idempotency_keys_job(only=tenant_id))  # type: ignore[no-any-return]


# ---------------------------------------------------------------- story 22.9 (A10): draft-resume cleanup
DRAFT_RESUME_DAYS = 7


async def purge_draft_resumes_job(now: datetime | None = None, only: str | None = None) -> dict[str, Any]:
    """Delete create-from-resume drafts (``candidate_resumes.candidate_id IS NULL``) older than 7 days and
    purge their stored files (tombstoned). Drafts whose file belongs to a bulk-import item are left alone."""
    from app.models.candidates import CandidateResume, ImportItem
    from app.services import files_service

    cutoff = (now or _now()) - timedelta(days=DRAFT_RESUME_DAYS)
    cond = and_(
        CandidateResume.candidate_id.is_(None),
        CandidateResume.created_at < cutoff,
        ~CandidateResume.file_id.in_(select(ImportItem.file_id).where(ImportItem.file_id.is_not(None))),
    )

    async def one(tid: uuid.UUID) -> int:
        async with tenant_session(tid) as db:
            rows = (
                await db.execute(
                    select(CandidateResume.id, CandidateResume.file_id).where(cond).limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()  # fmt: skip
            if not rows:
                return 0
            await db.execute(delete(CandidateResume).where(CandidateResume.id.in_([r.id for r in rows])))
            for r in rows:
                await files_service.purge_file(db, r.file_id)
            return len(rows)

    return await for_each_tenant(one, ALL_TENANTS, only=only)


@celery_app.task(name="candidates.purge_draft_resumes")  # type: ignore[untyped-decorator,unused-ignore]
def purge_draft_resumes(tenant_id: str | None = None) -> dict[str, Any]:
    return run(lambda: purge_draft_resumes_job(only=tenant_id))  # type: ignore[no-any-return]
