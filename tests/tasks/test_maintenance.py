"""A10 cleanup jobs (deferred-work items). Matrix row 31."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.database import platform_session, tenant_session
from app.core.security import hash_token, new_token
from app.models.identity import Invitation, PasswordReset, User
from app.models.identity import Session as AuthSession
from app.tasks import maintenance
from tests.factories import create_tenant_row


def _session(tenant_id: uuid.UUID, *, expires: datetime, revoked: datetime | None = None) -> AuthSession:
    now = datetime.now(UTC)
    return AuthSession(
        token_hash=hash_token(new_token()),
        principal_type="user",
        principal_id=uuid.uuid4(),
        tenant_id=tenant_id,
        created_at=now - timedelta(days=10),
        last_seen_at=now - timedelta(days=10),
        expires_at=expires,
        revoked_at=revoked,
    )


async def test_purge_sessions_keeps_active_ones() -> None:
    tenant_id = await create_tenant_row()
    now = datetime.now(UTC)
    rows = {
        "expired": _session(tenant_id, expires=now - timedelta(minutes=1)),
        "old_revoked": _session(tenant_id, expires=now + timedelta(days=1), revoked=now - timedelta(days=8)),
        "recent_revoked": _session(
            tenant_id, expires=now + timedelta(days=1), revoked=now - timedelta(days=1)
        ),
        "active": _session(tenant_id, expires=now + timedelta(days=1)),
    }
    async with platform_session() as db:
        db.add_all(rows.values())
    maintenance.purge_sessions.delay()
    async with platform_session() as db:
        left = set(
            (await db.execute(select(AuthSession.id).where(AuthSession.tenant_id == tenant_id))).scalars()
        )
    assert left == {rows["recent_revoked"].id, rows["active"].id}


async def test_purge_password_resets_and_invitations() -> None:
    tenant_id = await create_tenant_row()
    now = datetime.now(UTC)
    async with tenant_session(tenant_id) as db:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            email=f"{uuid.uuid4().hex[:8]}@x.example",
            name="U",
            password_hash="x",
        )
        db.add(user)
        await db.flush()

        def reset(expires: datetime, used: datetime | None = None) -> PasswordReset:
            return PasswordReset(
                tenant_id=tenant_id,
                user_id=user.id,
                token_hash=hash_token(new_token()),
                expires_at=expires,
                used_at=used,
            )

        keep = reset(now + timedelta(hours=1))
        db.add_all(
            [
                reset(now - timedelta(minutes=1)),
                reset(now + timedelta(hours=1), now - timedelta(hours=25)),
                keep,
            ]
        )

        def invitation(expires: datetime) -> Invitation:
            return Invitation(
                tenant_id=tenant_id, email=f"{uuid.uuid4().hex[:8]}@x.example", token_hash=hash_token(new_token()),  # noqa: E501
                expires_at=expires, invited_by_type="platform", invited_by_id=uuid.uuid4(),
            )  # fmt: skip

        recent_invite = invitation(now - timedelta(days=2))
        db.add_all([invitation(now - timedelta(days=31)), recent_invite])
    maintenance.purge_password_resets.delay(str(tenant_id))
    maintenance.purge_invitations.delay(str(tenant_id))
    maintenance.purge_invitations.delay(str(tenant_id))  # idempotent
    async with tenant_session(tenant_id) as db:
        assert set((await db.execute(select(PasswordReset.id))).scalars()) == {keep.id}
        assert set((await db.execute(select(Invitation.id))).scalars()) == {recent_invite.id}


async def test_link_and_api_log_purges_run() -> None:
    tenant_id = str(await create_tenant_row())
    assert maintenance.purge_secure_links.delay(tenant_id).get() == {tenant_id: {"lookups": 0, "links": 0}}
    assert maintenance.purge_api_key_request_log.delay(tenant_id).get() == {tenant_id: 0}
