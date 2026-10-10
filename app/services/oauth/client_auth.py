# ruff: noqa: E501
"""OAuth client authentication and the RFC 6749 error type (Epic 67, spec 0.2).

* ``client_secret_basic`` / ``client_secret_post`` for confidential clients (``sha256`` lookup in ``app_secrets``;
  unrevoked and unexpired, overlap secrets valid until ``expires_at``; constant-time compare); public clients send
  ``client_id`` only (PKCE is enforced by the code exchange).
* Failures: ``invalid_client`` (401 + ``WWW-Authenticate: Basic``), audited as ``oauth.client_auth_failed`` at most
  once per ``client_id`` per minute (the secret is never logged). Client rate limit 600/min (``oauth_token``).
"""

import dataclasses
import hmac
import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select

from app.core import audit, rate_limit
from app.core.database import platform_session
from app.models.oauth import App, AppSecret
from app.schemas.oauth import ClientAuth
from app.services.oauth.tokens import hash_token

LAST_USED_RESOLUTION = timedelta(minutes=1)
CLIENT_LIMIT = (600, 60)


class OAuthError(Exception):
    """An RFC 6749 error (``{"error", "error_description"}``), rendered by ``router/oauth.py`` (not the P1 envelope)."""

    def __init__(self, error: str, description: str = "", *, status: int = 400, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.status = status
        self.headers = headers or {}

    def body(self) -> dict[str, str]:
        out = {"error": self.error}
        if self.description:
            out["error_description"] = self.description
        return out


def invalid_client(description: str = "Client authentication failed") -> OAuthError:
    return OAuthError("invalid_client", description, status=401, headers={"WWW-Authenticate": 'Basic realm="talent-os"'})


@dataclasses.dataclass(frozen=True, slots=True)
class AuthedClient:
    app_id: uuid.UUID
    client_id: str
    client_type: str
    grant_types: tuple[str, ...]
    resources: tuple[str, ...]
    status: str
    name: str


def _now() -> datetime:
    return datetime.now(UTC)


async def _audit_failure(client_id: str, reason: str) -> None:
    key = client_id[:40] or "unknown"
    allowed, _ = await rate_limit.memory_backend().incr(f"oauth-audit:{key}", f"oauth-audit-prev:{key}", 1, 60_000, 0.0)
    if not allowed:
        return
    try:
        async with platform_session() as pdb:
            audit.record("oauth.client_auth_failed", "app", None, session=pdb, actor_type="app", actor_id=None,
                         client_id=key, reason=reason)  # fmt: skip
    except Exception:
        logger.exception("Could not audit an OAuth client failure")


async def authenticate_client(client: ClientAuth) -> AuthedClient:
    client_id = (client.client_id or "").strip()
    if not client_id or len(client_id) > 40:
        await _audit_failure(client_id, "malformed")
        raise invalid_client()
    result = await rate_limit.check("oauth_token:client", client_id, *CLIENT_LIMIT)
    if not result.allowed:
        raise OAuthError("slow_down", "Too many requests", status=429, headers=result.headers())
    now = _now()
    async with platform_session() as pdb:
        app = (await pdb.execute(select(App).where(App.client_id == client_id))).scalar_one_or_none()
        reason = None
        if app is None:
            reason = "unknown"
        elif app.status in ("suspended", "retired"):
            reason = app.status
        elif app.client_type == "confidential":
            if not client.client_secret:
                reason = "missing_secret"
            else:
                digest = hash_token(client.client_secret)
                rows = (
                    await pdb.execute(select(AppSecret).where(AppSecret.app_id == app.id, AppSecret.revoked_at.is_(None)))
                ).scalars()
                match = None
                for row in rows:
                    if hmac.compare_digest(bytes(row.secret_hash), digest) and (row.expires_at is None or row.expires_at > now):
                        match = row
                if match is None:
                    reason = "bad_secret"
                elif match.last_used_at is None or now - match.last_used_at >= LAST_USED_RESOLUTION:
                    match.last_used_at = now
        elif client.client_secret:
            reason = "public_with_secret"
        out = None
        if reason is None and app is not None:
            out = AuthedClient(app.id, app.client_id, app.client_type, tuple(app.grant_types), tuple(app.resources), app.status, app.name)
    if out is None:
        await _audit_failure(client_id, reason or "unknown")
        raise invalid_client()
    return out


__all__ = ["AuthedClient", "OAuthError", "authenticate_client", "invalid_client"]
