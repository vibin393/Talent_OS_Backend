# ruff: noqa: E501
"""Refresh-token rotation with reuse detection (Epic 67, story 67.4; spec 0.3).

Every use rotates: the old refresh token gets ``rotated_at`` and a new access + refresh pair is minted in the same
family (``issue_token(grant_type="refresh_token", family_id, parent_id)``; the family's absolute expiry is inherited).
A rotated (or revoked) refresh token presented again revokes the whole family (``reuse_detected``: the 66 event and
the ``oauth_refresh_reuse`` notification fire) and fails with ``invalid_grant``. Concurrent refreshes serialise on
``SELECT ... FOR UPDATE``: the loser sees ``rotated_at`` and is treated as reuse (clients must serialise refreshes).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core import audit
from app.core.database import platform_session, tenant_session
from app.core.errors import DomainError
from app.core.settings import get_settings
from app.models.oauth import AppInstallation, AppUserAuthorization, OAuthToken, OAuthTokenLookup
from app.schemas.oauth import IssuedTokens
from app.services import entity_refs
from app.services.oauth.client_auth import AuthedClient, OAuthError
from app.services.oauth.tokens import USABLE_INSTALLATION_STATUSES, hash_token, issue_token, revoke_for


def _now() -> datetime:
    return datetime.now(UTC)


def invalid_grant(description: str = "The grant is invalid, expired or revoked") -> OAuthError:
    return OAuthError("invalid_grant", description)


async def _revoke_family(tenant_id: uuid.UUID, family_id: uuid.UUID, installation_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        await revoke_for(db, family_id=family_id, reason="reuse_detected")
        audit.record("oauth.refresh_reuse_detected", "app_installation", installation_id, session=db, family_id=family_id)


async def refresh(client: AuthedClient, refresh_token: str | None, scope: str | None) -> IssuedTokens:
    if not refresh_token:
        raise OAuthError("invalid_request", "refresh_token is required")
    if "authorization_code" not in client.grant_types and "refresh_token" not in client.grant_types:
        raise OAuthError("unauthorized_client", "This client may not use refresh tokens")
    digest = hash_token(refresh_token)
    async with platform_session() as pdb:
        lookup = await pdb.get(OAuthTokenLookup, digest)
        tenant_id, token_id = (lookup.tenant_id, lookup.token_id) if lookup is not None and lookup.kind == "refresh" else (None, None)
    if tenant_id is None or token_id is None:
        raise invalid_grant()
    s = get_settings()
    now = _now()
    reuse: tuple[uuid.UUID, uuid.UUID] | None = None
    error: OAuthError | None = None
    issued: IssuedTokens | None = None
    async with tenant_session(tenant_id) as db:
        row = (await db.execute(select(OAuthToken).where(OAuthToken.id == token_id).with_for_update())).scalar_one_or_none()
        inst = await db.get(AppInstallation, row.installation_id) if row is not None else None
        if row is None or row.kind != "refresh" or inst is None:
            error = invalid_grant()
        elif inst.app_id != client.app_id:
            error = invalid_grant("The refresh token was not issued to this client")
        elif row.rotated_at is not None:
            reuse, error = (row.family_id, row.installation_id), invalid_grant("The refresh token was already used")
        elif row.revoked_at is not None:
            error = invalid_grant()
        elif (row.absolute_expires_at is not None and row.absolute_expires_at <= now) or (row.last_used_at or row.created_at) + timedelta(days=s.oauth_refresh_idle_days) <= now or row.expires_at <= now:
            error = invalid_grant("The refresh token expired")
        elif inst.status not in USABLE_INSTALLATION_STATUSES or row.user_id is None:
            error = invalid_grant("The app is no longer installed")
        elif await entity_refs.principal_for_user(db, tenant_id, row.user_id) is None:
            error = invalid_grant("The user is no longer active")
        else:
            auth = await db.get(AppUserAuthorization, row.user_authorization_id) if row.user_authorization_id else None
            if row.user_authorization_id is not None and (auth is None or auth.revoked_at is not None):
                error = invalid_grant("The authorization was revoked")
            else:
                wanted = sorted(set(scope.split())) if scope and scope.strip() else list(row.scopes)
                if not set(wanted) <= set(row.scopes):
                    error = OAuthError("invalid_scope", "A refresh can only narrow the scopes")
                else:
                    # Never carry scopes the user's authorisation or the Grant no longer holds.
                    live = set(inst.granted_scopes or []) & (set(auth.scopes) if auth is not None else set(wanted))
                    wanted = [k for k in wanted if k in live]
                    if not wanted:
                        error = invalid_grant("No scope of this grant is still allowed")
                    else:
                        row.rotated_at = now
                        row.last_used_at = now
                        await db.flush()
                        try:
                            issued = await issue_token(
                                db, installation_id=inst.id, grant_type="refresh_token",
                                resource=row.resource,  # type: ignore[arg-type]
                                scopes=wanted, user_id=row.user_id, user_authorization_id=row.user_authorization_id,
                                family_id=row.family_id, parent_id=row.id, with_refresh=True,
                            )  # fmt: skip
                        except DomainError as exc:
                            raise OAuthError("invalid_grant", exc.message) from exc
    if reuse is not None:
        await _revoke_family(tenant_id, *reuse)
    if error is not None:
        raise error
    assert issued is not None
    return issued


__all__ = ["invalid_grant", "refresh"]
