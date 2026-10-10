"""Calendar and mailbox connections (Epic 7, story 7.4, FR-53).

* ``list_providers`` -- ``[{provider, configured}]``; a provider is configured when its OAuth client id and
  secret are set, or ``CALENDAR_PROVIDER_FAKE`` is on.
* ``start`` -> ``{authorize_url}`` (409 ``provider_not_configured``). ``state`` is a random nonce + HMAC,
  stored for 10 minutes (single use) bound to the user and the session.
* ``complete_callback`` validates the state, exchanges the code, stores the tokens in the Secrets Vault
  (Epic 66: owner ``calendar_connection`` = connection id, names ``access_token`` / ``refresh_token``) and
  returns the
  frontend redirect (``/settings/connections?connected=<provider>`` or ``?error=<code>``).
* ``disconnect`` -- ``provider.revoke`` (errors logged, ignored), vault tokens deleted, status ``revoked``.
* ``get_active(db, user_id)`` -> ``ActiveConnection | None``; refreshes when the access token expires within
  2 minutes; a refresh failure marks the row ``needs_reconnect`` and returns None (callers fall back).
* ``free_busy`` -- per user ``available`` (their own connected calendar) or ``unknown``; cached 2 minutes.
* ``send_mail_as`` -- the ``core.mailer`` routing hook (a user's connected mailbox).
* Epic 72 (D-18, additive registries; P1 behaviour is unchanged while nothing is registered):
  ``register_event_written_listener`` (called by ``interviews.invites`` after each provider write),
  ``set_event_tag_provider`` (the version tag of ``EventSpec.tos_tag``), ``register_connection_listener``
  (connected / reconnected / needs_reconnect / revoked) and ``register_free_busy_fallback`` (users whose own
  free/busy is ``unknown``).

Tokens are never returned, logged or audited. Connect / reconnect / disconnect call ``audit.record``.
"""

import dataclasses
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext, get_context
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.mailer import EmailMessage
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.models.connections import UserConnection
from app.schemas.connections import (
    BusyOut,
    ConnectionOut,
    FreeBusyIn,
    FreeBusyOut,
    ProviderOut,
    StartOut,
    UserFreeBusyOut,
)
from app.services import vault_service
from app.services.connections import PROVIDERS, get_provider, is_configured
from app.services.connections.provider import (
    BusyBlock,
    CalendarProvider,
    ProviderAuthError,
    ProviderError,
    TokenSet,
)
from app.services.connections.store import get_store

STATE_TTL_SECONDS = 600
FREE_BUSY_TTL_SECONDS = 120
REFRESH_MARGIN = timedelta(minutes=2)
MAX_FREE_BUSY_RANGE = timedelta(days=14)


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================ Epic 72: additive registries (D-18)
EventWrittenListener = Callable[..., Awaitable[None]]
ConnectionListener = Callable[[AsyncSession, uuid.UUID, uuid.UUID, str], Awaitable[None]]
BusyMap = dict[uuid.UUID, list[BusyBlock]]
FreeBusyFallback = Callable[
    [AsyncSession, uuid.UUID, list[uuid.UUID], datetime, datetime], Awaitable[BusyMap]
]
_EVENT_WRITTEN: list[EventWrittenListener] = []
_CONNECTION_LISTENERS: list[ConnectionListener] = []
_FREE_BUSY_FALLBACKS: list[FreeBusyFallback] = []
_TAG_PROVIDER: list[Callable[[uuid.UUID, int], str]] = []


def register_event_written_listener(fn: EventWrittenListener) -> None:
    """Call ``fn(db, *, tenant_id, interview_id, connection_id, provider, provider_event_id, ical_uid,
    sequence, tos_tag, etag, cancelled, starts_at, ends_at)`` after each provider write (interview event)."""
    if fn not in _EVENT_WRITTEN:
        _EVENT_WRITTEN.append(fn)


def set_event_tag_provider(fn: Callable[[uuid.UUID, int], str] | None) -> None:
    """``fn(interview_id, sequence) -> tag`` for ``EventSpec.tos_tag``; None removes it (P1 behaviour)."""
    _TAG_PROVIDER.clear()
    if fn is not None:
        _TAG_PROVIDER.append(fn)


def event_tag(interview_id: uuid.UUID, sequence: int) -> str | None:
    return _TAG_PROVIDER[0](interview_id, sequence) if _TAG_PROVIDER else None


def register_connection_listener(fn: ConnectionListener) -> None:
    """Call ``fn(db, connection_id, user_id, status)`` (``active`` / ``needs_reconnect`` / ``revoked``)."""
    if fn not in _CONNECTION_LISTENERS:
        _CONNECTION_LISTENERS.append(fn)


def register_free_busy_fallback(fn: FreeBusyFallback) -> None:
    """Call ``fn(db, tenant_id, user_ids, start, end) -> {user_id: busy}`` (own result unknown)."""
    if fn not in _FREE_BUSY_FALLBACKS:
        _FREE_BUSY_FALLBACKS.append(fn)


async def notify_event_written(db: AsyncSession, **data: Any) -> None:
    for fn in list(_EVENT_WRITTEN):
        try:
            async with db.begin_nested():
                await fn(db, **data)
        except Exception:  # a listener never breaks the P1 invite path
            logger.exception("event-written listener failed")


async def _notify_connection(db: AsyncSession, row: UserConnection) -> None:
    for fn in list(_CONNECTION_LISTENERS):
        try:
            async with db.begin_nested():
                await fn(db, row.id, row.user_id, row.status)
        except Exception:
            logger.exception("connection listener failed")


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _check_provider(name: str) -> str:
    if name not in PROVIDERS:
        raise NotFound("Unknown provider", code="provider_unknown")
    return name


def redirect_uri(provider: str) -> str:
    return f"{get_settings().oauth_redirect_base.rstrip('/')}/api/v1/connections/{provider}/callback"


def _frontend(query: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/settings/connections?{query}"


def _out(row: UserConnection) -> ConnectionOut:
    return ConnectionOut(
        id=row.id,
        provider=row.provider,
        account_email=row.account_email,
        scopes=list(row.scopes or []),
        status=row.status,  # type: ignore[arg-type]
        connected_at=row.connected_at,
        expires_at=row.expires_at,
        last_error=row.last_error,
    )


# ================================================================ providers / start / callback
def list_providers() -> list[ProviderOut]:
    out: list[ProviderOut] = []
    for name in PROVIDERS:
        impl = get_provider(name)
        scopes = list(getattr(impl, "scopes", ()) or ())
        if name == "google" and not scopes:
            from app.services.connections.provider import GOOGLE_SCOPES

            scopes = list(GOOGLE_SCOPES)
        if name == "microsoft" and not scopes:
            from app.services.connections.provider import MICROSOFT_SCOPES

            scopes = list(MICROSOFT_SCOPES)
        out.append(ProviderOut(provider=name, configured=is_configured(name), scopes=scopes))  # type: ignore[arg-type]
    return out


def _state_secret() -> bytes:
    s = get_settings()
    return hashlib.sha256(f"talent-os-oauth-state:{s.token_encryption_key or 'dev'}".encode()).digest()


def _sign(nonce: str) -> str:
    return hmac.new(_state_secret(), nonce.encode(), hashlib.sha256).hexdigest()[:32]


def _state_key(nonce: str) -> str:
    return f"oauth_state:{hashlib.sha256(nonce.encode()).hexdigest()}"


async def start(ctx: RequestContext, provider: str, *, reply_capture: bool = False) -> StartOut:
    """Begin the OAuth flow. Epic 33: ``reply_capture`` adds the opt-in reply-capture scopes (re-consent)."""
    principal = _principal(ctx)
    _check_provider(provider)
    impl = get_provider(provider)
    if impl is None:
        raise ConflictError("This provider is not configured", code="provider_not_configured")
    extra: tuple[str, ...] = ()
    if reply_capture:
        from app.services.connections.provider import REPLY_SCOPES

        extra = REPLY_SCOPES.get(getattr(impl, "name", provider), REPLY_SCOPES[provider])
    nonce = secrets.token_urlsafe(32)
    payload = {
        "user_id": str(principal.id),
        "tenant_id": str(principal.tenant_id),
        "session_id": str(principal.session_id) if principal.session_id else None,
        "provider": provider,
    }
    await get_store().put(_state_key(nonce), json.dumps(payload), STATE_TTL_SECONDS)
    state = f"{nonce}.{_sign(nonce)}"
    if extra:
        return StartOut(authorize_url=impl.authorize_url(state, redirect_uri(provider), extra_scopes=extra))
    return StartOut(authorize_url=impl.authorize_url(state, redirect_uri(provider)))


async def _consume_state(state: str | None, principal: Principal, provider: str) -> bool:
    if not state or "." not in state or len(state) > 300:
        return False
    nonce, _, sig = state.rpartition(".")
    if not hmac.compare_digest(sig, _sign(nonce)):
        return False
    raw = await get_store().pop(_state_key(nonce))  # single use, even when the rest fails
    if raw is None:
        return False
    data = json.loads(raw)
    session_id = str(principal.session_id) if principal.session_id else None
    return (
        data.get("user_id") == str(principal.id)
        and data.get("tenant_id") == str(principal.tenant_id)
        and data.get("session_id") == session_id
        and data.get("provider") == provider
    )


async def _live_row(db: AsyncSession, user_id: uuid.UUID, provider: str) -> UserConnection | None:
    return (
        await db.execute(
            select(UserConnection)
            .where(
                UserConnection.user_id == user_id,
                UserConnection.provider == provider,
                UserConnection.status != "revoked",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def complete_callback(
    ctx: RequestContext,
    db: AsyncSession,
    provider: str,
    code: str | None,
    state: str | None,
    error: str | None = None,
) -> str:
    """Finish the OAuth dance; returns the frontend URL to redirect the browser to."""
    principal = _principal(ctx)
    if provider not in PROVIDERS:
        return _frontend("error=provider_unknown")
    if not await _consume_state(state, principal, provider):
        return _frontend("error=invalid_state")
    if error:
        return _frontend("error=access_denied")
    impl = get_provider(provider)
    if impl is None:
        return _frontend("error=provider_not_configured")
    if not code:
        return _frontend("error=missing_code")
    try:
        tokens = await impl.exchange_code(code, redirect_uri(provider))
    except ProviderError as exc:
        logger.warning("OAuth code exchange failed (provider={}, error={})", provider, type(exc).__name__)
        return _frontend("error=exchange_failed")
    row = await _live_row(db, principal.id, provider)
    action = "connection.reconnected" if row is not None else "connection.connected"
    if row is None:
        row = UserConnection(
            id=uuid.uuid4(),
            tenant_id=_tenant(db),
            user_id=principal.id,
            provider=provider,
            connected_at=_now(),
        )
        db.add(row)
    row.account_email = (tokens.account_email or "").lower() or None
    row.scopes = list(tokens.scopes or impl.scopes)
    row.expires_at = tokens.expires_at
    row.status = "active"
    row.connected_at = _now()
    row.last_error = None
    await db.flush()
    await _store_tokens(ctx, db, row.id, tokens)
    audit.record(action, "user_connection", row.id, session=db, provider=provider)
    await _notify_connection(db, row)
    return _frontend(f"connected={provider}")


# ================================================================ list / disconnect
async def list_mine(ctx: RequestContext, db: AsyncSession) -> list[ConnectionOut]:
    principal = _principal(ctx)
    rows = (
        await db.execute(
            select(UserConnection)
            .where(UserConnection.user_id == principal.id, UserConnection.status != "revoked")
            .order_by(UserConnection.provider)
        )
    ).scalars()
    return [_out(r) for r in rows]


TOKEN_OWNER = "calendar_connection"


async def _store_tokens(
    ctx: RequestContext, db: AsyncSession, connection_id: uuid.UUID, tokens: TokenSet
) -> None:
    """Write the OAuth tokens to the vault; a missing refresh token keeps the stored one (refresh responses
    often omit it)."""
    ref = vault_service.SecretRef
    await vault_service.put(ctx, db, ref(TOKEN_OWNER, connection_id, "access_token"), tokens.access_token)
    if tokens.refresh_token:
        refresh = ref(TOKEN_OWNER, connection_id, "refresh_token")
        await vault_service.put(ctx, db, refresh, tokens.refresh_token)


async def _token_set(db: AsyncSession, row: UserConnection) -> TokenSet | None:
    bag = await vault_service.open_bag(get_context(), db, TOKEN_OWNER, row.id, purpose="calendar")
    access = bag.get("access_token")
    if access is None:
        return None
    return TokenSet(
        access_token=access,
        refresh_token=bag.get("refresh_token"),
        expires_at=row.expires_at,
        scopes=tuple(row.scopes or ()),
        account_email=row.account_email,
    )


async def disconnect(ctx: RequestContext, db: AsyncSession, connection_id: uuid.UUID) -> ConnectionOut:
    principal = _principal(ctx)
    row = (
        await db.execute(
            select(UserConnection)
            .where(UserConnection.id == connection_id, UserConnection.user_id == principal.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Connection not found")
    if row.status != "revoked":
        tokens = await _token_set(db, row)
        impl = get_provider(row.provider)
        if tokens is not None and impl is not None:
            try:
                await impl.revoke(tokens)
            except Exception as exc:  # FR-53: revoke is best effort; the tokens are wiped regardless
                logger.warning("Provider revoke failed (connection={}, error={})", row.id, type(exc).__name__)
        await vault_service.delete_owner(ctx, db, TOKEN_OWNER, row.id)
        row.expires_at = None
        row.status = "revoked"
        await db.flush()
        audit.record("connection.disconnected", "user_connection", row.id, session=db, provider=row.provider)
        await _notify_connection(db, row)
    return _out(row)


# ================================================================ token use
@dataclasses.dataclass(frozen=True, slots=True)
class ActiveConnection:
    connection_id: uuid.UUID
    provider: str  # "google" | "microsoft" (Epic 10 reads this name)
    impl: CalendarProvider
    token_set: TokenSet
    account_email: str | None


async def mark_needs_reconnect(db: AsyncSession, connection_id: uuid.UUID, error: str) -> None:
    row = await db.get(UserConnection, connection_id)
    if row is None or row.status == "revoked":
        return
    row.status = "needs_reconnect"
    row.last_error = error[:200]
    await db.flush()
    audit.record("connection.needs_reconnect", "user_connection", row.id, session=db, provider=row.provider)
    await _notify_connection(db, row)


async def get_active(
    db: AsyncSession, user_id: uuid.UUID | None, provider: str | None = None
) -> ActiveConnection | None:
    """Return the user's usable connection (refreshed when needed), else None. Never raises for provider
    errors."""
    if user_id is None:
        return None
    stmt = select(UserConnection).where(UserConnection.user_id == user_id, UserConnection.status == "active")
    if provider is not None:
        stmt = stmt.where(UserConnection.provider == provider)
    row = (await db.execute(stmt.order_by(UserConnection.connected_at.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return None
    impl = get_provider(row.provider)
    tokens = await _token_set(db, row)
    if impl is None or tokens is None:
        return None
    if tokens.expires_at is None or tokens.expires_at < _now() + REFRESH_MARGIN:
        try:
            tokens = await impl.refresh(tokens)
        except ProviderError as exc:
            logger.warning("Token refresh failed (connection={}, error={})", row.id, type(exc).__name__)
            await mark_needs_reconnect(db, row.id, "refresh_failed")
            return None
        await _store_tokens(get_context(), db, row.id, tokens)
        row.expires_at = tokens.expires_at
        await db.flush()
    return ActiveConnection(row.id, row.provider, impl, tokens, row.account_email)


async def has_active(db: AsyncSession, user_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(UserConnection.id).where(
                UserConnection.user_id == user_id, UserConnection.status == "active"
            )
        )
    ).first()
    return row is not None


# ================================================================ free / busy
def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def free_busy(ctx: RequestContext, db: AsyncSession, data: FreeBusyIn) -> FreeBusyOut:
    _principal(ctx)
    start, end = _as_utc(data.start_utc), _as_utc(data.end_utc)
    if end <= start:
        raise ValidationFailed("end_utc must be after start_utc", code="invalid_range")
    if end - start > MAX_FREE_BUSY_RANGE:
        raise ValidationFailed("The range may cover at most 14 days", code="range_too_long")
    tenant_id = _tenant(db)
    store = get_store()
    items: list[UserFreeBusyOut] = []
    for user_id in dict.fromkeys(data.user_ids):
        key = f"freebusy:{tenant_id}:{user_id}:{start.isoformat()}:{end.isoformat()}"
        cached = await store.get(key)
        if cached is not None:
            items.append(UserFreeBusyOut.model_validate_json(cached))
            continue
        item = UserFreeBusyOut(user_id=user_id, status="unknown", busy=[])
        conn = await get_active(db, user_id)
        if conn is not None and conn.account_email:
            try:
                result = await conn.impl.free_busy(conn.token_set, [conn.account_email], start, end)
                blocks: list[BusyBlock] = result.get(conn.account_email.lower(), [])
                item = UserFreeBusyOut(
                    user_id=user_id,
                    status="available",
                    busy=[BusyOut(start=_as_utc(b.start), end=_as_utc(b.end)) for b in blocks],
                )
            except ProviderAuthError:
                await mark_needs_reconnect(db, conn.connection_id, "free_busy_unauthorized")
            except ProviderError as exc:
                logger.warning(
                    "Free/busy lookup failed (connection={}, error={})",
                    conn.connection_id,
                    type(exc).__name__,
                )
        if item.status == "unknown" and _FREE_BUSY_FALLBACKS:
            item = await _fallback(db, tenant_id, user_id, start, end) or item
        await store.put(key, item.model_dump_json(), FREE_BUSY_TTL_SECONDS)
        items.append(item)
    return FreeBusyOut(items=items)


async def _fallback(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, start: datetime, end: datetime
) -> UserFreeBusyOut | None:
    """Epic 72: the organisation free/busy source for a user without a usable personal connection."""
    for fn in list(_FREE_BUSY_FALLBACKS):
        try:
            found = await fn(db, tenant_id, [user_id], start, end)
        except Exception as exc:
            logger.warning("Free/busy fallback failed (error={})", type(exc).__name__)
            continue
        if user_id in found:
            busy = [BusyOut(start=_as_utc(b.start), end=_as_utc(b.end)) for b in found[user_id]]
            return UserFreeBusyOut(user_id=user_id, status="available", busy=busy, source="organisation")
    return None


# ================================================================ mailbox (core.mailer routing hook + Epic 10)
async def send_mail_as(tenant_id: uuid.UUID, user_id: uuid.UUID, message: EmailMessage) -> bool:
    """Send ``message`` through ``user_id``'s connected mailbox. False = no usable mailbox (caller falls back)."""
    from app.core.database import tenant_session

    async with tenant_session(tenant_id) as db:
        conn = await get_active(db, user_id)
        if conn is None:
            return False
        try:
            await conn.impl.send_mail(conn.token_set, message)
        except ProviderAuthError:
            await mark_needs_reconnect(db, conn.connection_id, "send_unauthorized")
            return False
        except ProviderError as exc:
            logger.warning(
                "Mailbox send failed (connection={}, error={})", conn.connection_id, type(exc).__name__
            )
            return False
    return True


def register() -> None:
    """Install the mailbox route of ``core.mailer`` (idempotent)."""
    from app.core import mailer

    mailer.set_mailbox_sender(send_mail_as)


register()
