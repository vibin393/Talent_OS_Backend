# ruff: file-ignore[line-too-long]
"""Shared webhook helpers (Epic 68): the §0.1 type/scope rule, the §0.2 filters, owner resolution, row loading, the
DTO mapping, the echo-URL test and the deduplicated after-commit pump kick."""

import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from loguru import logger
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import oauth_scopes
from app.core import public_events as registry
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import Scope
from app.core.settings import get_settings
from app.models.oauth import AppInstallation
from app.models.webhooks import WebhookSubscription
from app.schemas.webhooks import SigningSecretMeta, SubscriptionOut

WILDCARD = "*"
WEBHOOK_FILTER_FIELDS = frozenset(
    {"client_id", "demand_id", "legal_entity_id", "placement_id", "candidate_id", "to_stage", "to_status", "status"}
)
WILDCARD_FILTER_FIELDS = frozenset({"client_id", "legal_entity_id"})
INSTALLATION_ONLY_TYPES = frozenset({"app.installation_changed", "subject.erased"})
# Known gap 2: ``proposal.decided`` cannot be routed to the creating Installation yet (ProposalOut has no
# ``app_installation_id``) -- hidden from pickers ("Coming soon") and never delivered.
UNROUTABLE_TYPES = frozenset({"proposal.decided"})
SECRET_NAME = "signing_secret"
OWNER_TYPE = "webhook_subscription"
ECHO_PATH = "/api/v1/webhook-echo/"


def now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ owners
def principal_installation(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    if getattr(p, "type", None) == "app":
        return getattr(p, "app_installation_id", None) or getattr(p, "id", None)
    return None


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    if getattr(p, "type", None) == "user":
        return getattr(p, "id", None)
    if getattr(p, "type", None) == "app":
        return getattr(p, "acting_user_id", None)
    return None


async def installation(db: AsyncSession, installation_id: uuid.UUID) -> AppInstallation | None:
    return (await db.execute(select(AppInstallation).where(AppInstallation.id == installation_id))).scalar_one_or_none()


# ------------------------------------------------------------------ §0.1 type / scope rule
def non_sensitive(scopes: Iterable[str]) -> set[str]:
    return {s for s in scopes if not oauth_scopes.is_sensitive(s)}


def public_types() -> Mapping[str, registry.PublicEventSpec]:
    return registry.catalogue()


def installation_allows(event_type: str, granted: Iterable[str]) -> bool:
    if event_type in UNROUTABLE_TYPES:
        return False
    if event_type in INSTALLATION_ONLY_TYPES:
        return True
    spec = public_types().get(event_type)
    if spec is None or not spec.scopes:
        return False
    return bool(non_sensitive(granted) & set(spec.scopes))


def user_allows(event_type: str, principal: Any) -> bool:
    if event_type in UNROUTABLE_TYPES or event_type in INSTALLATION_ONLY_TYPES:
        return False
    spec = public_types().get(event_type)
    if spec is None or not spec.scopes or principal is None:
        return False
    for key in spec.scopes:
        perms = oauth_scopes.permissions_for([key])
        if perms and all(principal.has(p, Scope.TENANT) for p in perms):
            return True
    return False


def requires_label(event_type: str) -> list[str]:
    spec = public_types().get(event_type)
    return list(spec.scopes) if spec is not None else []


def tenant_wildcard_types() -> set[str]:
    return {t for t, s in public_types().items() if s.scopes and t not in UNROUTABLE_TYPES and t not in INSTALLATION_ONLY_TYPES}


def type_matches(sub: WebhookSubscription, event_type: str) -> bool:
    return bool(sub.all_events) or event_type in (sub.event_types or [])


# ------------------------------------------------------------------ §0.2 filters
def _fields_of(event_type: str) -> set[str]:
    spec = public_types().get(event_type)
    return set(spec.data_model.model_fields) if spec is not None else set()  # type: ignore[attr-defined]


def allowed_filter_fields(event_types: list[str], all_events: bool) -> set[str]:
    if all_events:
        return set(WILDCARD_FILTER_FIELDS)
    if not event_types:
        return set()
    common = set.intersection(*(_fields_of(t) for t in event_types))
    return common & WEBHOOK_FILTER_FIELDS


def validate_filters(filters: Mapping[str, list[str]], event_types: list[str], all_events: bool) -> dict[str, list[str]]:
    allowed = allowed_filter_fields(event_types, all_events)
    unknown = sorted(set(filters) - allowed)
    if unknown:
        raise ValidationFailed(
            "Unknown filter field", code="invalid_filter", details={"fields": unknown, "allowed": sorted(allowed)}
        )
    return {k: [str(v) for v in vals] for k, vals in filters.items()}


def filters_match(filters: Mapping[str, list[str]], data: Mapping[str, Any]) -> bool:
    for key, values in (filters or {}).items():
        if key not in data or data[key] is None:
            return False
        if str(data[key]) not in {str(v) for v in values}:
            return False
    return True


# ------------------------------------------------------------------ URLs
def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def echo_prefix() -> str:
    return get_settings().p5_public_webhook_base_url() + ECHO_PATH


def is_echo_url(url: str) -> bool:
    return url.startswith(echo_prefix())


def echo_token_of(url: str) -> str | None:
    if not is_echo_url(url):
        return None
    return url[len(echo_prefix()) :].split("?", 1)[0].strip("/") or None


# ------------------------------------------------------------------ loading / visibility
async def load(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID, *, include_deleted: bool = False, lock: bool = False) -> WebhookSubscription:
    stmt = select(WebhookSubscription).where(WebhookSubscription.id == subscription_id)
    own = principal_installation(ctx)
    if own is not None:
        stmt = stmt.where(WebhookSubscription.installation_id == own)
    if not include_deleted:
        stmt = stmt.where(WebhookSubscription.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update()
    sub = (await db.execute(stmt)).scalar_one_or_none()
    if sub is None:
        raise NotFound("Webhook subscription not found")
    return sub


async def secret_meta(ctx: RequestContext, db: AsyncSession, sub: WebhookSubscription) -> SigningSecretMeta:
    from app.services import vault_service

    meta = {m.name: m for m in await vault_service.meta(ctx, db, OWNER_TYPE, sub.id)}
    cur = meta.get(SECRET_NAME)
    prev = meta.get(SECRET_NAME + vault_service.PREVIOUS_SUFFIX)
    return SigningSecretMeta(
        fingerprint=cur.fingerprint if cur else None,
        rotated_at=(cur.rotated_at if cur else None) or sub.secret_rotated_at,
        previous_expires_at=prev.expires_at if prev else None,
    )


def version_of(sub: WebhookSubscription) -> str:
    stamp = sub.updated_at or sub.created_at
    return str(int(stamp.timestamp() * 1000)) if stamp else "0"


async def out(ctx: RequestContext, db: AsyncSession, sub: WebhookSubscription, *, paused: bool = False) -> SubscriptionOut:
    return SubscriptionOut(
        id=sub.id, owner_kind=sub.owner_kind, installation_id=sub.installation_id, url=sub.url, url_host=sub.url_host,
        description=sub.description, event_types=list(sub.event_types or []), all_events=bool(sub.all_events),
        filters=dict(sub.filters or {}), status=sub.status, disabled_reason=sub.disabled_reason, paused=paused,
        verified_at=sub.verified_at, verification_error=sub.verification_error, max_in_flight=sub.max_in_flight,
        suppress_own_changes=sub.suppress_own_changes, consecutive_failures=sub.consecutive_failures,
        failure_streak_started_at=sub.failure_streak_started_at, last_success_at=sub.last_success_at,
        last_failure_at=sub.last_failure_at, disabled_at=sub.disabled_at, signing_secret=await secret_meta(ctx, db, sub),
        created_at=sub.created_at, updated_at=sub.updated_at, version=version_of(sub),
    )  # fmt: skip


# ------------------------------------------------------------------ after-commit pump kick (deduplicated 1 s)
_kick_lock = threading.Lock()
_kick_until: dict[str, float] = {}
AUTO_KICK = {"on": True}  # tests switch the automatic pump off to drive the engine step by step


def _acquire(tenant_id: uuid.UUID) -> bool:
    key = str(tenant_id)
    t = time.monotonic()
    with _kick_lock:
        if _kick_until.get(key, 0.0) > t:
            return False
        _kick_until[key] = t + 1.0
        return True


def kick_pump(tenant_id: uuid.UUID) -> None:
    if not AUTO_KICK["on"]:
        return
    from app.tasks import webhooks as tasks
    from app.tasks.runtime import enqueue

    task = tasks.pump
    eager = bool(getattr(getattr(getattr(task, "app", None), "conf", None), "task_always_eager", False))
    if not eager and not _acquire(tenant_id):
        return
    try:
        enqueue(task, str(tenant_id))
    except Exception:  # pragma: no cover - never fail the caller
        logger.exception("Could not enqueue webhooks.pump for tenant {}", tenant_id)


def kick_after_commit(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    sync = db.sync_session
    key = "webhooks_kick"
    if sync.info.get(key):
        return
    sync.info[key] = True

    def _after(_session: Any) -> None:
        sync.info.pop(key, None)
        kick_pump(tenant_id)

    def _rollback(_session: Any) -> None:
        sync.info.pop(key, None)

    sa_event.listen(sync, "after_commit", _after, once=True)
    sa_event.listen(sync, "after_soft_rollback", _rollback, once=True)


__all__ = [
    "ECHO_PATH",
    "OWNER_TYPE",
    "SECRET_NAME",
    "WEBHOOK_FILTER_FIELDS",
    "WILDCARD",
    "allowed_filter_fields",
    "echo_prefix",
    "echo_token_of",
    "filters_match",
    "installation",
    "installation_allows",
    "is_echo_url",
    "kick_after_commit",
    "kick_pump",
    "load",
    "now",
    "out",
    "principal_installation",
    "strip_query",
    "type_matches",
    "user_allows",
    "user_id_of",
    "validate_filters",
]
