# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Subscriptions (Epic 68, story 68.1, §0.1/§0.2): CRUD, the scope and filter rules, the 25 / 100 limits and the
Egress Guard at save (syntax only; ``SafeHttpClient`` re-checks every connection). Public callers (App principals)
own Installation Subscriptions and only ever see their own (others -> 404). Secrets are returned once."""

import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, events
from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.net_safety import EgressRefused, validate_url
from app.core.permissions import Scope
from app.core.public_api import decode_cursor, encode_cursor
from app.core.settings import get_settings
from app.core.tenancy import is_sandbox
from app.events.webhooks import WebhookSubscriptionChanged
from app.models.oauth import AppInstallation
from app.models.webhooks import WebhookDelivery, WebhookSubscription
from app.schemas.public_events import PublicPage
from app.schemas.webhooks import (
    EventTypeOut,
    SecretRotated,
    SubscriptionCreate,
    SubscriptionCreated,
    SubscriptionOut,
    SubscriptionQuery,
    SubscriptionUpdate,
)
from app.services.webhooks import common, signing


# ------------------------------------------------------------------ helpers
def changed(db: AsyncSession, sub: WebhookSubscription, change: str) -> None:
    events.emit(
        db,
        WebhookSubscriptionChanged(
            tenant_id=sub.tenant_id, subscription_id=sub.id, owner_kind=sub.owner_kind, installation_id=sub.installation_id,
            change=change, status=sub.status, disabled_reason=sub.disabled_reason,
        ),
    )  # fmt: skip


def audit_sub(db: AsyncSession, action: str, sub: WebhookSubscription, **data: Any) -> None:
    audit.record(f"webhook.{action}", "webhook_subscription", sub.id, session=db, url=common.strip_query(sub.url), owner_kind=sub.owner_kind, **data)


async def _normalise_url(ctx: RequestContext, raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if common.is_echo_url(raw):
        if not get_settings().webhook_echo_enabled or not await is_sandbox(ctx.tenant_id):
            raise ValidationFailed("The echo endpoint can only be used in a sandbox", code="echo_sandbox_only")
        return raw, (urlsplit(raw).hostname or "echo").lower()[:253]
    try:
        parsed = validate_url(raw)
    except EgressRefused as exc:
        raise ValidationFailed(
            "This address can't be used. Use a public https address.", code="egress_refused", details={"reason": exc.reason}
        ) from None
    if len(parsed.url) > 2000:
        raise ValidationFailed("The URL is too long", code="invalid_url")
    return parsed.url, parsed.host


async def _owner(ctx: RequestContext, db: AsyncSession, requested: uuid.UUID | None) -> tuple[str, AppInstallation | None]:
    own = common.principal_installation(ctx)
    if own is not None:
        if requested is not None:
            raise ValidationFailed("installation_id is not accepted here", code="validation_failed")
        inst = await common.installation(db, own)
        if inst is None or inst.status == "uninstalled":
            raise NotFound("Installation not found")
        return "installation", inst
    if requested is None:
        return "tenant", None
    principal = ctx.principal
    if principal is None or not principal.has("integration:manage", Scope.TENANT):
        raise Forbidden("Creating a webhook for an app needs integration:manage", code="forbidden", required_permission="integration:manage")
    inst = await common.installation(db, requested)
    if inst is None or inst.status == "uninstalled" or inst.kind != "app":
        raise NotFound("Installation not found")
    return "installation", inst


def _check_types(ctx: RequestContext, types: list[str], inst: AppInstallation | None) -> tuple[list[str], bool]:
    all_events = common.WILDCARD in types
    if all_events:
        if len(types) != 1:
            raise ValidationFailed("Use either * or a list of event types", code="invalid_event_types")
        if inst is None:
            missing = sorted(t for t in common.tenant_wildcard_types() if not common.user_allows(t, ctx.principal))
            if missing:
                raise ValidationFailed(
                    "You can't receive every event type; select the events you need", code="event_type_not_allowed",
                    details={"types": missing},
                )  # fmt: skip
        return [], True
    catalogue = common.public_types()
    bad = sorted({t for t in types if t not in catalogue})
    if bad:
        raise ValidationFailed("Unknown event type", code="invalid_event_types", details={"types": bad})
    if inst is not None:
        refused = sorted({t for t in types if not common.installation_allows(t, inst.granted_scopes or [])})
    else:
        refused = sorted({t for t in types if not common.user_allows(t, ctx.principal)})
    if refused:
        raise ValidationFailed("Event type not allowed for this owner", code="event_type_not_allowed", details={"types": refused})
    return sorted(set(types)), False


async def _check_limits(db: AsyncSession, inst: AppInstallation | None) -> None:
    s = get_settings()
    live = select(func.count()).select_from(WebhookSubscription).where(WebhookSubscription.deleted_at.is_(None))
    if (await db.execute(live)).scalar_one() >= s.webhook_max_per_tenant:
        raise ConflictError("This organisation already has the maximum number of webhooks", code="subscription_limit")
    if inst is not None:
        n = (await db.execute(live.where(WebhookSubscription.installation_id == inst.id))).scalar_one()
        if n >= s.webhook_max_per_installation:
            raise ConflictError("This app already has the maximum number of webhooks", code="subscription_limit")


async def paused(ctx: RequestContext, db: AsyncSession, sub: WebhookSubscription) -> bool:
    """The kill switch / a platform or App suspension pauses (never drops) deliveries."""
    from app.services import ecosystem_service

    assert ctx.tenant_id is not None
    app_id = None
    if sub.installation_id is not None:
        inst = await common.installation(db, sub.installation_id)
        if inst is not None and inst.status == "suspended":
            return True
        app_id = inst.app_id if inst is not None else None
    state = await ecosystem_service.is_suspended(ctx.tenant_id, app_id=app_id)
    return bool(state.suspended)


# ------------------------------------------------------------------ reads
async def list_subscriptions(ctx: RequestContext, db: AsyncSession, q: SubscriptionQuery) -> PublicPage[SubscriptionOut]:
    stmt = select(WebhookSubscription)
    own = common.principal_installation(ctx)
    if own is not None:
        stmt = stmt.where(WebhookSubscription.installation_id == own)
    elif q.installation_id is not None:
        stmt = stmt.where(WebhookSubscription.installation_id == q.installation_id)
    if not q.include_deleted:
        stmt = stmt.where(WebhookSubscription.deleted_at.is_(None))
    if q.status:
        stmt = stmt.where(WebhookSubscription.status == q.status)
    if q.cursor:
        (created,), last = decode_cursor(q.cursor)
        from datetime import datetime

        stmt = stmt.where(tuple_(WebhookSubscription.created_at, WebhookSubscription.id) > tuple_(datetime.fromisoformat(str(created)), last))
    rows = list((await db.execute(stmt.order_by(WebhookSubscription.created_at, WebhookSubscription.id).limit(q.limit + 1))).scalars())
    has_more = len(rows) > q.limit
    rows = rows[: q.limit]
    is_paused = False
    if rows:
        from app.services import ecosystem_service

        assert ctx.tenant_id is not None
        is_paused = (await ecosystem_service.is_suspended(ctx.tenant_id)).suspended
    data = [await common.out(ctx, db, r, paused=is_paused) for r in rows]
    nxt = encode_cursor((rows[-1].created_at,), rows[-1].id) if has_more and rows else None
    return PublicPage[SubscriptionOut](data=data, next_cursor=nxt, has_more=has_more)


async def get_subscription(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> SubscriptionOut:
    sub = await common.load(ctx, db, subscription_id)
    return await common.out(ctx, db, sub, paused=await paused(ctx, db, sub))


async def allowed_event_types(ctx: RequestContext, db: AsyncSession, *, installation_id: uuid.UUID | None) -> list[EventTypeOut]:
    own = common.principal_installation(ctx)
    inst = None
    if own is not None or installation_id is not None:
        inst = await common.installation(db, own or installation_id)  # type: ignore[arg-type]
        if inst is None:
            raise NotFound("Installation not found")
    out: list[EventTypeOut] = []
    for t, spec in sorted(common.public_types().items()):
        if inst is not None:
            ok = common.installation_allows(t, inst.granted_scopes or [])
        else:
            ok = common.user_allows(t, ctx.principal)
        out.append(
            EventTypeOut(
                type=t, version=spec.version, description=spec.description, scopes=list(spec.scopes), allowed=ok,
                requires=["coming_soon"] if t in common.UNROUTABLE_TYPES else ([] if ok else list(spec.scopes)),
                schema=spec.data_model.model_json_schema(), example=dict(spec.example or {}),
            )
        )  # fmt: skip
    return out


# ------------------------------------------------------------------ mutations
async def create_subscription(ctx: RequestContext, db: AsyncSession, data: SubscriptionCreate) -> SubscriptionCreated:
    kind, inst = await _owner(ctx, db, data.installation_id)
    url, host = await _normalise_url(ctx, data.url)
    types, all_events = _check_types(ctx, data.event_types, inst)
    filters = common.validate_filters(data.filters, types, all_events)
    await _check_limits(db, inst)
    assert ctx.tenant_id is not None
    user = common.user_id_of(ctx)
    sub = WebhookSubscription(
        id=uuid.uuid4(), tenant_id=ctx.tenant_id, owner_kind=kind, installation_id=inst.id if inst else None, url=url,
        url_host=host, description=data.description, event_types=types, all_events=all_events, filters=filters,
        status="pending_verification", max_in_flight=data.max_in_flight,
        suppress_own_changes=bool(data.suppress_own_changes) and inst is not None, created_by_user_id=user,
        created_by_installation_id=common.principal_installation(ctx),
        notify_user_id=user or (inst.accountable_user_id if inst else None),
    )  # fmt: skip
    db.add(sub)
    await db.flush()
    secret = await signing.store(ctx, db, sub.id)
    audit_sub(db, "subscription_created", sub, event_types=types, all_events=all_events)
    changed(db, sub, "created")
    await db.flush()
    await db.refresh(sub)
    return SubscriptionCreated(subscription=await common.out(ctx, db, sub), signing_secret=secret)


async def update_subscription(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID, data: SubscriptionUpdate) -> SubscriptionOut:
    sub = await common.load(ctx, db, subscription_id, lock=True)
    inst = await common.installation(db, sub.installation_id) if sub.installation_id else None
    fields = data.model_fields_set
    reverify = False
    if "url" in fields and data.url is not None:
        url, host = await _normalise_url(ctx, data.url)
        if url != sub.url:
            sub.url, sub.url_host = url, host
            reverify = sub.status != "disabled"
    types, all_events = list(sub.event_types or []), bool(sub.all_events)
    if "event_types" in fields and data.event_types is not None:
        types, all_events = _check_types(ctx, data.event_types, inst)
        sub.event_types, sub.all_events = types, all_events
    if ("filters" in fields and data.filters is not None) or "event_types" in fields:
        sub.filters = common.validate_filters(data.filters if data.filters is not None else dict(sub.filters or {}), types, all_events)
    if "description" in fields:
        sub.description = data.description
    if "max_in_flight" in fields and data.max_in_flight is not None:
        sub.max_in_flight = data.max_in_flight
    if "suppress_own_changes" in fields and data.suppress_own_changes is not None:
        sub.suppress_own_changes = bool(data.suppress_own_changes) and inst is not None
    if reverify:
        sub.status, sub.verified_at, sub.verification_error, sub.verification_attempts = "pending_verification", None, None, 0
    audit_sub(db, "subscription_updated", sub, fields=sorted(fields), reverify=reverify)
    changed(db, sub, "updated")
    await db.flush()
    await db.refresh(sub)
    return await common.out(ctx, db, sub)


async def _cancel_open(db: AsyncSession, subscription_id: uuid.UUID) -> int:
    result = await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.subscription_id == subscription_id, WebhookDelivery.status.in_(("pending", "retrying", "held")))
        .values(status="cancelled", next_attempt_at=None, updated_at=common.now())
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_sub_row(ctx: RequestContext, db: AsyncSession, sub: WebhookSubscription, *, reason: str | None = None) -> None:
    if reason is not None and sub.status != "disabled":
        sub.status, sub.disabled_reason, sub.disabled_at = "disabled", reason, common.now()
    sub.deleted_at = common.now()
    await _cancel_open(db, sub.id)
    await signing.delete_all(ctx, db, sub.id)
    audit_sub(db, "subscription_deleted", sub, reason=reason)
    changed(db, sub, "deleted")


async def delete_subscription(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> None:
    sub = await common.load(ctx, db, subscription_id, lock=True)
    await delete_sub_row(ctx, db, sub)
    await db.flush()


async def disable(db: AsyncSession, sub: WebhookSubscription, reason: str) -> None:
    """Turn a Subscription off; its open deliveries become ``held`` (replayable for 30 days)."""
    if sub.status == "disabled":
        return
    sub.status, sub.disabled_reason, sub.disabled_at = "disabled", reason, common.now()
    await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.subscription_id == sub.id, WebhookDelivery.status.in_(("pending", "retrying")))
        .values(status="held", updated_at=common.now())
        .execution_options(synchronize_session=False)
    )
    audit_sub(db, "subscription_disabled", sub, reason=reason)
    changed(db, sub, "disabled")


async def set_enabled(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID, *, enabled: bool) -> SubscriptionOut:
    sub = await common.load(ctx, db, subscription_id, lock=True)
    if not enabled:
        await disable(db, sub, "manual")
    elif sub.status == "disabled":
        stale = sub.verified_at is None or (common.now() - sub.verified_at).days > 30
        sub.disabled_reason, sub.disabled_at = None, None
        sub.consecutive_failures, sub.failure_streak_started_at, sub.streak_notified_at = 0, None, None
        if stale:
            sub.status, sub.verification_attempts, sub.verification_error = "pending_verification", 0, None
        else:
            sub.status = "active"
        audit_sub(db, "subscription_enabled", sub, reverify=stale)
        changed(db, sub, "enabled")
    await db.flush()
    await db.refresh(sub)
    return await common.out(ctx, db, sub)


async def rotate_secret(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> SecretRotated:
    sub = await common.load(ctx, db, subscription_id, lock=True)
    value, expires = await signing.rotate(ctx, db, sub.id)
    sub.secret_rotated_at = common.now()
    audit_sub(db, "secret_rotated", sub)
    changed(db, sub, "secret_rotated")
    await db.flush()
    await db.refresh(sub)
    return SecretRotated(subscription=await common.out(ctx, db, sub), signing_secret=value, previous_expires_at=expires)


async def apply_installation_change(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID, change: str) -> int:
    """``InstallationChanged``: uninstalled -> delete its Subscriptions; granted -> drop types no longer allowed
    (none left -> disabled ``scope_lost``). Returns the Subscriptions touched."""
    subs = list(
        (await db.execute(select(WebhookSubscription).where(WebhookSubscription.installation_id == installation_id, WebhookSubscription.deleted_at.is_(None)).with_for_update())).scalars()
    )
    if not subs:
        return 0
    inst = await common.installation(db, installation_id)
    touched = 0
    if change == "uninstalled" or inst is None or inst.status == "uninstalled":
        for sub in subs:
            await delete_sub_row(ctx, db, sub, reason="uninstalled")
            touched += 1
        return touched
    if change not in ("granted", "upgraded", "reconsent_required"):
        return 0
    granted = inst.granted_scopes or []
    for sub in subs:
        if sub.all_events:
            continue
        keep = [t for t in sub.event_types or [] if common.installation_allows(t, granted)]
        if keep == list(sub.event_types or []):
            continue
        touched += 1
        if keep:
            sub.event_types = keep
            sub.filters = {k: v for k, v in (sub.filters or {}).items() if k in common.allowed_filter_fields(keep, False)}
            audit_sub(db, "subscription_updated", sub, fields=["event_types"], reason="scope_lost")
            changed(db, sub, "updated")
        else:
            await disable(db, sub, "scope_lost")
    return touched


__all__ = [
    "allowed_event_types",
    "apply_installation_change",
    "audit_sub",
    "changed",
    "create_subscription",
    "delete_sub_row",
    "delete_subscription",
    "disable",
    "get_subscription",
    "list_subscriptions",
    "paused",
    "rotate_secret",
    "set_enabled",
    "update_subscription",
]
