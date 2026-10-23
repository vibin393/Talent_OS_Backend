"""Communication service (Epic 10, story 10.1; FR-52, FR-54).

* **Templates** -- ``email_templates`` + language ``email_template_variants`` (launch: ``en`` only, anything
  else 422 ``language_not_supported``). One system template per key is seeded (migration 101, tenant
  provisioning and lazily on first use); system templates are editable but not archivable (409
  ``template_system``). Saving validates merge tokens (``merge_fields``) and sanitises the body (nh3).
  ``render_template(ctx, db, key, language, context)`` is the API for other epics.
* **Sending** -- ``send_emails`` (``POST /emails``) and ``queue_email`` (workflow / system callers) commit
  ``queued`` rows; ``EmailQueued`` (after commit) enqueues ``comms.send_email(tenant_id, message_id)``, whose
  body is ``deliver``: the sender's connected mailbox when there is one (pluggable ``MailboxProvider`` hook;
  Epic 7 provides the real one), else the platform mailer ("<User> via <Tenant>", reply-to the user,
  ``fallback_reason`` recorded). Success logs an ``email`` activity (subject + recipients, never the body) on
  every related and recipient entity and emits ``EmailSent``; after 3 retries the message is ``failed``
  (``EmailFailed``).
* **Bounces** -- ``record_bounce`` (webhook, mailbox adapters, dev CLI) marks recipients, records hard
  bounces per address, logs an activity and emits ``EmailBounced``. A hard bounce never blocks sending: it
  is returned as a warning.
"""

import dataclasses
import hashlib
import hmac
import inspect
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any, Protocol

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import mailer
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, NotFound, RateLimited, Unauthorized, ValidationFailed
from app.core.events import emit, subscribe
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.events.comms import EmailBounced, EmailFailed, EmailQueued, EmailSent
from app.models.comms import Email, EmailAddressStatus, EmailRecipient, EmailTemplate, EmailTemplateVariant
from app.schemas.comms import (
    AddressStatusOut,
    BounceIn,
    BounceOut,
    ContextRefs,
    EmailDetailOut,
    EmailOut,
    EmailSendIn,
    EmailSendOut,
    MergeFieldGroupOut,
    PreviewIn,
    PreviewOut,
    RecipientIn,
    RecipientOut,
    RenderedTemplate,
    SendWarnings,
    TemplateCreate,
    TemplateDetailOut,
    TemplateOut,
    TemplateUpdate,
    VariantIn,
    VariantOut,
)
from app.services import entity_refs
from app.services.comms import merge_fields
from app.services.comms.defaults import DEFAULT_TEMPLATES
from app.services.comms.html import html_to_text, sanitize_html

SUPPORTED_TEMPLATE_LANGUAGES: tuple[str, ...] = ("en",)
MAX_ATTEMPTS = 4  # first try + 3 retries
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (30, 120, 600)
RECIPIENT_ENTITY_TYPES = ("candidate", "contact")


def _now() -> datetime:
    return datetime.now(UTC)


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


def platform_address() -> str:
    return parseaddr(get_settings().mail_from)[1] or "no-reply@talentos.local"


# ================================================================ mailbox hook (Epic 7)
class ConnectionExpired(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    """Raised by a mailbox provider when the user's OAuth connection can no longer send."""


@dataclasses.dataclass(frozen=True, slots=True)
class SendConnection:
    provider: str  # "google" | "microsoft"
    account_email: str | None = None
    handle: Any = None  # provider-specific (token set, connection row, ...)


class MailboxProvider(Protocol):
    async def get_send_connection(
        self, ctx: RequestContext, db: AsyncSession, user_id: uuid.UUID
    ) -> SendConnection | None: ...

    async def send_mail(self, conn: SendConnection, message: mailer.EmailMessage) -> str | None: ...


class _ServiceMailbox:
    """Adapter over ``app.services.mailbox_service`` (Epic 7) once it exists:
    ``get_send_connection(ctx, user_id) -> Connection | None`` and ``send_mail(conn, message) -> id``."""

    def __init__(self, module: Any) -> None:
        self.module = module

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def get_send_connection(
        self,
        ctx: RequestContext,
        db: AsyncSession,
        user_id: uuid.UUID,
    ) -> SendConnection | None:
        conn = await self._maybe_await(self.module.get_send_connection(ctx, user_id))
        if conn is None:
            return None
        provider = str(getattr(conn, "provider", "") or "")
        return SendConnection(
            provider=provider, account_email=getattr(conn, "account_email", None), handle=conn
        )

    async def send_mail(self, conn: SendConnection, message: mailer.EmailMessage) -> Any:
        # Epic 33: may be a ``SentRef`` (provider id + thread ref) -- see ``_sent_id``.
        return await self._maybe_await(self.module.send_mail(conn.handle, message))


_mailbox_override: MailboxProvider | None = None


def set_mailbox_provider(provider: MailboxProvider | None) -> None:
    """Install a mailbox provider (tests, Epic 7 wiring). ``None`` = discover ``mailbox_service``."""
    global _mailbox_override  # process-wide wiring
    _mailbox_override = provider


def mailbox_provider() -> MailboxProvider | None:
    if _mailbox_override is not None:
        return _mailbox_override
    module = entity_refs.optional_service("app.services.mailbox_service")
    if module is not None and hasattr(module, "get_send_connection") and hasattr(module, "send_mail"):
        return _ServiceMailbox(module)
    return None


def _sent_id(result: Any) -> str | None:
    """Epic 33: the provider message id of a mailbox send result (``SentRef`` or a plain id)."""
    if result is None:
        return None
    ref = getattr(result, "provider_message_id", None)
    if ref is not None or hasattr(result, "thread_ref"):
        return None if ref is None else str(ref)
    return str(result)


def _is_expired(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionExpired) or type(exc).__name__ == "ConnectionExpired"


# ================================================================ templates
def _language_check(language: str) -> str:
    lang = language.strip().lower()
    if lang not in SUPPORTED_TEMPLATE_LANGUAGES:
        raise ValidationFailed(
            f"Language '{language}' is not supported yet",
            code="language_not_supported",
            details={"supported": list(SUPPORTED_TEMPLATE_LANGUAGES)},
        )
    return lang


async def ensure_default_templates(db: AsyncSession, tenant_id: uuid.UUID | None = None) -> int:
    """Insert the missing system templates of the session's tenant (idempotent). Returns how many."""
    tenant_id = tenant_id or entity_refs.session_tenant(db)
    existing = set(
        (await db.execute(select(EmailTemplate.key).where(EmailTemplate.is_system.is_(True)))).scalars()
    )
    created = 0
    for key, spec in DEFAULT_TEMPLATES.items():
        if key in existing:
            continue
        try:
            async with db.begin_nested():
                with entity_refs.acting_as(db, "system"):
                    template = EmailTemplate(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        key=key,
                        name=spec["name"],
                        description=spec["description"],
                        is_system=True,
                    )
                    db.add(template)
                    await db.flush()
                    db.add(
                        EmailTemplateVariant(
                            id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            template_id=template.id,
                            language="en",
                            subject=spec["subject"],
                            body_html=sanitize_html(spec["body_html"]),
                            version=1,
                        )
                    )
                    await db.flush()
            created += 1
        except (IntegrityError, DBAPIError):  # concurrent seeding, or a read-only (support) session
            logger.debug("Default template {} not seeded for tenant {}", key, tenant_id)
    return created


async def _variants(
    db: AsyncSession, template_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[EmailTemplateVariant]]:
    out: dict[uuid.UUID, list[EmailTemplateVariant]] = {t: [] for t in template_ids}
    if template_ids:
        rows = await db.execute(
            select(EmailTemplateVariant)
            .where(EmailTemplateVariant.template_id.in_(list(template_ids)))
            .order_by(EmailTemplateVariant.language)
        )
        for v in rows.scalars():
            out.setdefault(v.template_id, []).append(v)
    return out


def _template_out(t: EmailTemplate, variants: Sequence[EmailTemplateVariant]) -> TemplateOut:
    latest = max([t.updated_at, *(v.updated_at for v in variants)])
    return TemplateOut(
        id=t.id,
        key=t.key,
        name=t.name,
        description=t.description,
        is_system=t.is_system,
        languages=[v.language.strip() for v in variants],
        archived_at=t.archived_at,
        created_at=t.created_at,
        updated_at=latest,
    )


def _variant_out(v: EmailTemplateVariant) -> VariantOut:
    return VariantOut(
        id=v.id,
        language=v.language.strip(),
        subject=v.subject,
        body_html=v.body_html,
        version=v.version,
        updated_at=v.updated_at,
    )


async def _detail(db: AsyncSession, t: EmailTemplate) -> TemplateDetailOut:
    await db.refresh(t)
    variants = (await _variants(db, [t.id]))[t.id]
    for v in variants:
        await db.refresh(v)
    base = _template_out(t, variants)
    return TemplateDetailOut(**base.model_dump(), variants=[_variant_out(v) for v in variants])


async def list_templates(
    ctx: RequestContext, db: AsyncSession, key: str | None = None, include_archived: bool = False
) -> list[TemplateOut]:
    _principal(ctx)
    await ensure_default_templates(db)
    stmt = select(EmailTemplate)
    if key:
        stmt = stmt.where(EmailTemplate.key == key)
    if not include_archived:
        stmt = stmt.where(EmailTemplate.archived_at.is_(None))
    rows = list(
        (
            await db.execute(
                stmt.order_by(EmailTemplate.is_system.desc(), EmailTemplate.key, EmailTemplate.name)
            )
        )
        .scalars()
        .all()
    )
    variants = await _variants(db, [t.id for t in rows])
    return [_template_out(t, variants.get(t.id, [])) for t in rows]


async def _get_template(
    db: AsyncSession, template_id: uuid.UUID, *, for_update: bool = False
) -> EmailTemplate:
    stmt = select(EmailTemplate).where(EmailTemplate.id == template_id)
    if for_update:
        stmt = stmt.with_for_update()
    template = (await db.execute(stmt)).scalar_one_or_none()
    if template is None:
        raise NotFound("Email template not found")
    return template


async def get_template(ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID) -> TemplateDetailOut:
    _principal(ctx)
    return await _detail(db, await _get_template(db, template_id))


def _template_exists() -> ConflictError:
    return ConflictError("A template with this type and name already exists", code="template_exists")


async def create_template(ctx: RequestContext, db: AsyncSession, data: TemplateCreate) -> TemplateDetailOut:
    principal = _principal(ctx)
    language = _language_check(data.language)
    merge_fields.validate_tokens(data.key, data.subject, data.body_html)
    template = EmailTemplate(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        key=data.key,
        name=data.name,
        description=data.description,
        is_system=False,
    )
    try:
        async with db.begin_nested():
            db.add(template)
            await db.flush()
    except IntegrityError as exc:
        raise _template_exists() from exc
    db.add(
        EmailTemplateVariant(
            id=uuid.uuid4(),
            tenant_id=principal.tenant_id,
            template_id=template.id,
            language=language,
            subject=data.subject,
            body_html=sanitize_html(data.body_html),
            version=1,
        )
    )
    await db.flush()
    return await _detail(db, template)


async def update_template(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, data: TemplateUpdate
) -> TemplateDetailOut:
    _principal(ctx)
    template = await _get_template(db, template_id, for_update=True)
    sent = data.model_fields_set
    try:
        async with db.begin_nested():
            if "name" in sent and data.name is not None:
                template.name = data.name
            if "description" in sent:
                template.description = data.description
            await db.flush()
    except IntegrityError as exc:
        raise _template_exists() from exc
    return await _detail(db, template)


async def put_variant(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID, language: str, data: VariantIn
) -> TemplateDetailOut:
    principal = _principal(ctx)
    lang = _language_check(language)
    template = await _get_template(db, template_id, for_update=True)
    merge_fields.validate_tokens(template.key, data.subject, data.body_html)
    body = sanitize_html(data.body_html)
    variant = (
        await db.execute(
            select(EmailTemplateVariant)
            .where(EmailTemplateVariant.template_id == template.id, EmailTemplateVariant.language == lang)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if variant is None:
        db.add(
            EmailTemplateVariant(
                id=uuid.uuid4(),
                tenant_id=principal.tenant_id,
                template_id=template.id,
                language=lang,
                subject=data.subject,
                body_html=body,
                version=1,
            )
        )
    elif variant.subject != data.subject or variant.body_html != body:
        variant.subject = data.subject
        variant.body_html = body
        variant.version = variant.version + 1
    await db.flush()
    return await _detail(db, template)


async def archive_template(
    ctx: RequestContext, db: AsyncSession, template_id: uuid.UUID
) -> TemplateDetailOut:
    _principal(ctx)
    template = await _get_template(db, template_id, for_update=True)
    if template.is_system:
        raise ConflictError("System templates can be edited but not removed", code="template_system")
    if template.archived_at is None:
        template.archived_at = _now()
        await db.flush()
    return await _detail(db, template)


def merge_field_catalog(key: str | None) -> list[MergeFieldGroupOut]:
    return [MergeFieldGroupOut.model_validate(g) for g in merge_fields.catalog_for(key)]


async def _check_refs(ctx: RequestContext, db: AsyncSession, refs: ContextRefs) -> dict[str, Any]:
    """Every referenced entity must be readable by the caller (404 otherwise)."""
    checks = {
        "candidate_id": "candidate",
        "contact_id": "contact",
        "client_id": "client",
        "demand_id": "demand",
        "application_id": "application",
    }
    values = refs.model_dump(exclude_none=True)
    for field, entity_type in checks.items():
        if field in values:
            await entity_refs.require_readable(ctx, db, entity_type, values[field])
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in values.items()}


def _render(subject: str, body: str, data: merge_fields.MergeData) -> tuple[str, str, list[str]]:
    rendered_subject, missing_subject = merge_fields.render(subject, data, escape=False)
    rendered_body, missing_body = merge_fields.render(body, data, escape=True)
    warnings = list(dict.fromkeys([*missing_subject, *missing_body]))
    return rendered_subject.strip(), sanitize_html(rendered_body), warnings


async def preview(ctx: RequestContext, db: AsyncSession, data: PreviewIn) -> PreviewOut:
    """Render with sample values (no ``context_refs``) or with real entities; persists nothing."""
    principal = _principal(ctx)
    merge_fields.validate_tokens(data.key, data.subject, data.body_html)
    body = sanitize_html(data.body_html)
    refs = data.context_refs
    if refs is None or not refs.model_dump(exclude_none=True):
        values: merge_fields.MergeData = merge_fields.sample_data()
        used_samples = True
    else:
        checked = await _check_refs(ctx, db, refs)
        values = await entity_refs.merge_data(db, principal.tenant_id, checked, sender_user_id=principal.id)  # type: ignore[arg-type]
        used_samples = False
    subject, rendered, warnings = _render(data.subject, body, values)
    return PreviewOut(
        subject=subject, body_html=rendered, render_warnings=warnings, used_samples=used_samples
    )


async def active_template(db: AsyncSession, key: str) -> EmailTemplate | None:
    await ensure_default_templates(db)
    return (
        await db.execute(
            select(EmailTemplate)
            .where(EmailTemplate.key == key, EmailTemplate.archived_at.is_(None))
            .order_by(EmailTemplate.is_system.desc(), EmailTemplate.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _variant_for(
    db: AsyncSession, template_id: uuid.UUID, language: str
) -> EmailTemplateVariant | None:
    rows = {
        v.language.strip(): v
        for v in (
            await db.execute(
                select(EmailTemplateVariant).where(EmailTemplateVariant.template_id == template_id)
            )
        ).scalars()
    }
    return rows.get(language) or rows.get("en") or next(iter(rows.values()), None)


async def render_template(
    ctx: RequestContext,
    db: AsyncSession,
    key: str,
    language: str = "en",
    context: Mapping[str, Any] | None = None,
) -> RenderedTemplate | None:
    """The tenant's active template for ``key`` rendered with ``context`` (``candidate_id``, ``demand_id``,
    ``contact_id``, ``client_id``, ``interview_id``, ``offer_id``, ``task_id``, ``link_url``,
    ``sender_user_id``, ``recipient`` (entity_type, entity_id)). None when the tenant has no such template, so
    callers fall back to their built-in text."""  # ruff: ignore[non-imperative-mood]
    template = await active_template(db, key)
    if template is None:
        return None
    variant = await _variant_for(db, template.id, language)
    if variant is None:
        return None
    context = dict(context or {})
    tenant_id = entity_refs.session_tenant(db)
    sender = context.pop("sender_user_id", None) or getattr(ctx.principal, "id", None)
    recipient = None
    if isinstance(context.get("recipient"), Mapping):
        ref = context.pop("recipient")
        recipient = await entity_refs.addressee(db, ref["entity_type"], uuid.UUID(str(ref["entity_id"])))
    values = await entity_refs.merge_data(
        db, tenant_id, context, sender_user_id=uuid.UUID(str(sender)) if sender else None, recipient=recipient
    )
    subject, body, warnings = _render(variant.subject, variant.body_html, values)
    return RenderedTemplate(
        template_id=template.id,
        variant_id=variant.id,
        version=variant.version,
        language=variant.language.strip(),
        subject=subject,
        body_html=body,
        render_warnings=warnings,
    )


# ================================================================ sending
@dataclasses.dataclass(frozen=True, slots=True)
class RecipientSpec:
    kind: str  # to | cc
    address: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    full_name: str | None = None

    def addressee(self) -> entity_refs.Addressee | None:
        if self.entity_type is None or self.entity_id is None:
            return None
        return entity_refs.Addressee(self.entity_type, self.entity_id, self.address, self.full_name)


@dataclasses.dataclass(frozen=True, slots=True)
class QueueResult:
    message_ids: list[uuid.UUID]
    warnings: SendWarnings


def _missing_email(entity_type: str, entity_id: uuid.UUID) -> ValidationFailed:
    return ValidationFailed(
        "The recipient has no email address",
        code="recipient_email_missing",
        details={"entity_type": entity_type, "entity_id": str(entity_id)},
    )


async def recipient_for(
    db: AsyncSession, entity_type: str, entity_id: uuid.UUID, kind: str = "to"
) -> RecipientSpec:
    """Resolve a candidate / contact to a recipient (404 when missing, 422 without an email)."""
    who = await entity_refs.addressee(db, entity_type, entity_id)
    if who is None:
        raise NotFound("Recipient not found")
    if not who.address:
        raise _missing_email(entity_type, entity_id)
    return RecipientSpec(kind, who.address, entity_type, entity_id, who.full_name)


async def _resolve(
    ctx: RequestContext, db: AsyncSession, items: Iterable[RecipientIn], kind: str
) -> list[RecipientSpec]:
    out: list[RecipientSpec] = []
    seen: set[str] = set()
    for item in items:
        if item.entity_type is not None and item.entity_id is not None:
            await entity_refs.require_readable(ctx, db, item.entity_type, item.entity_id)
            spec = await recipient_for(db, item.entity_type, item.entity_id, kind)
        else:
            spec = RecipientSpec(kind, str(item.address).strip().lower())
        if spec.address not in seen:
            seen.add(spec.address)
            out.append(spec)
    return out


async def _rate_limit(db: AsyncSession, user_id: uuid.UUID, new_messages: int) -> None:
    limit = get_settings().email_send_rate_per_user_hour
    since = _now() - timedelta(hours=1)
    sent = (
        await db.execute(
            select(func.count())
            .select_from(Email)
            .where(Email.sender_user_id == user_id, Email.origin == "user", Email.created_at >= since)
        )
    ).scalar_one()
    if int(sent) + new_messages > limit:
        raise RateLimited(
            "Too many emails sent in the last hour; try again later",
            code="rate_limited",
            details={"limit_per_hour": limit},
        )


async def _address_statuses(db: AsyncSession, addresses: Iterable[str]) -> dict[str, EmailAddressStatus]:
    addrs = sorted({a.lower() for a in addresses})
    if not addrs:
        return {}
    rows = await db.execute(select(EmailAddressStatus).where(EmailAddressStatus.address.in_(addrs)))
    return {str(r.address).lower(): r for r in rows.scalars()}


async def queue_email(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    origin: str,
    to: Sequence[RecipientSpec],
    subject: str,
    body_html: str,
    sender_user_id: uuid.UUID | None = None,
    cc: Sequence[RecipientSpec] = (),
    category: str = "process",
    related: Sequence[tuple[str, uuid.UUID]] = (),
    refs: Mapping[str, Any] | None = None,
    template_variant_id: uuid.UUID | None = None,
    template_version: int | None = None,
    workflow_execution_id: uuid.UUID | None = None,
    reply_to_user_id: uuid.UUID | None = None,
    raw: bool = False,
) -> QueueResult:
    """Create ``queued`` messages (one per ``to`` recipient when recipient merge fields are used) and emit
    ``EmailQueued``. ``raw=True`` skips merge rendering (pre-rendered system messages). The caller's session
    commits; sending happens after commit in Celery."""
    if not to:
        raise ValidationFailed("At least one recipient is required", code="recipient_required")
    tenant_id = entity_refs.session_tenant(db)
    body = sanitize_html(body_html)
    used = [] if raw else merge_fields.tokens(subject) + merge_fields.tokens(body)
    split = bool(merge_fields.groups_of(used) & merge_fields.RECIPIENT_GROUPS) and len(to) > 1
    batches: list[list[RecipientSpec]] = [[r] for r in to] if split else [list(to)]
    if origin == "user" and sender_user_id is not None:
        await _rate_limit(db, sender_user_id, len(batches))

    statuses = await _address_statuses(db, [r.address for r in [*to, *cc]])
    bounced = sorted(a for a, s in statuses.items() if s.hard_bounced_at is not None)
    opted_out = {a for a, s in statuses.items() if s.marketing_opt_out_at is not None}
    suppressed: set[str] = opted_out & {r.address for r in [*to, *cc]} if category == "marketing" else set()

    sender = await entity_refs.user(db, sender_user_id)
    reply_user = await entity_refs.user(db, reply_to_user_id or sender_user_id)
    from_address = sender.email if (origin == "user" and sender is not None) else platform_address()
    related_json = [{"entity_type": t, "entity_id": str(i)} for t, i in dict.fromkeys(related)]

    message_ids: list[uuid.UUID] = []
    queued: list[uuid.UUID] = []
    render_warnings: list[str] = []
    for batch in batches:
        if raw:
            rendered_subject, rendered_body = subject.strip(), body
        else:
            recipient = batch[0].addressee() if len(batch) == 1 else None
            values = await entity_refs.merge_data(
                db, tenant_id, refs or {}, sender_user_id=sender_user_id, recipient=recipient
            )
            rendered_subject, rendered_body, warnings = _render(subject, body, values)
            render_warnings.extend(w for w in warnings if w not in render_warnings)
        all_suppressed = all(r.address in suppressed for r in batch)
        message = Email(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            sender_user_id=sender_user_id,
            transport="platform",
            from_address=from_address,
            reply_to=reply_user.email if reply_user is not None else None,
            subject=rendered_subject[:500] or "(no subject)",
            body_html=rendered_body,
            body_text=html_to_text(rendered_body),
            template_variant_id=template_variant_id,
            template_version=template_version,
            category=category,
            status="failed" if all_suppressed else "queued",
            error_code="all_recipients_suppressed" if all_suppressed else None,
            related=related_json,
            origin=origin,
            workflow_execution_id=workflow_execution_id,
        )
        db.add(message)
        await db.flush()
        for r in [*batch, *cc]:
            db.add(
                EmailRecipient(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    message_id=message.id,
                    kind=r.kind,
                    address=r.address,
                    entity_type=r.entity_type,
                    entity_id=r.entity_id,
                    status="suppressed" if r.address in suppressed else "pending",
                )
            )
        message_ids.append(message.id)
        if not all_suppressed:
            queued.append(message.id)
    await db.flush()
    if queued:
        emit(db, EmailQueued(tenant_id=tenant_id, message_ids=tuple(queued)))
    return QueueResult(
        message_ids=message_ids,
        warnings=SendWarnings(
            bounced_addresses=bounced,
            suppressed_addresses=sorted(suppressed),
            render_warnings=render_warnings,
        ),
    )


async def send_emails(ctx: RequestContext, db: AsyncSession, data: EmailSendIn) -> EmailSendOut:
    """``POST /emails``: validate, sanitise, render per recipient and queue (202)."""
    principal = _principal(ctx)
    key, variant_id, variant_version = "custom", None, None
    if data.template_variant_id is not None:
        variant = await db.get(EmailTemplateVariant, data.template_variant_id)
        if variant is None:
            raise NotFound("Email template not found")
        template = await _get_template(db, variant.template_id)
        key, variant_id, variant_version = template.key, variant.id, variant.version
    merge_fields.validate_tokens(key, data.subject, data.body_html)
    related: list[tuple[str, uuid.UUID]] = []
    for ref in data.related:
        await entity_refs.require_readable(ctx, db, ref.entity_type, ref.entity_id)
        related.append((ref.entity_type, ref.entity_id))
    refs = await _check_refs(ctx, db, data.context_refs)
    to = await _resolve(ctx, db, data.to, "to")
    cc = [c for c in await _resolve(ctx, db, data.cc, "cc") if c.address not in {t.address for t in to}]
    result = await queue_email(
        ctx,
        db,
        origin="user",
        sender_user_id=principal.id,
        to=to,
        cc=cc,
        subject=data.subject,
        body_html=data.body_html,
        category=data.category,
        related=related,
        refs=refs,
        template_variant_id=variant_id,
        template_version=variant_version,
    )
    return EmailSendOut(message_ids=result.message_ids, warnings=result.warnings)


async def address_status(
    ctx: RequestContext, db: AsyncSession, addresses: Sequence[str]
) -> list[AddressStatusOut]:
    _principal(ctx)
    wanted = list(dict.fromkeys(a.strip().lower() for a in addresses if a.strip()))[:50]
    statuses = await _address_statuses(db, wanted)
    return [
        AddressStatusOut(
            address=a,
            hard_bounced_at=statuses[a].hard_bounced_at if a in statuses else None,
            last_bounce_reason=statuses[a].last_bounce_reason if a in statuses else None,
            marketing_opt_out=a in statuses and statuses[a].marketing_opt_out_at is not None,
        )
        for a in wanted
    ]


# ================================================================ delivery (Celery task body)
# Epic 39 (additive registries; ``comms`` never imports ``campaigns``): a category gate decides, immediately before
# the transport, whether a message of that category may still go out (``gate(db, message) -> obj`` with ``.action``
# in send | suppress | defer and ``.reason``); a header provider adds headers (List-Unsubscribe) per category.
CategoryGate = Any
HeaderProvider = Any
_category_gates: dict[str, CategoryGate] = {}
_header_providers: dict[str, HeaderProvider] = {}


def register_category_gate(category: str, gate: CategoryGate) -> None:
    _category_gates[category] = gate


def register_header_provider(category: str, provider: HeaderProvider) -> None:
    """``provider(db, message) -> Mapping[str, str]`` (async) merged into the outgoing headers."""
    _header_providers[category] = provider


async def set_marketing_opt_out(db: AsyncSession, address: str, at: datetime | None = None) -> None:
    """Record a marketing opt-out for ``address`` (``email_address_status``; survives erasure as before)."""
    tenant_id = entity_refs.session_tenant(db)
    addr = address.strip().lower()
    status = await db.get(EmailAddressStatus, (tenant_id, addr))
    when = at or _now()
    if status is None:
        db.add(EmailAddressStatus(tenant_id=tenant_id, address=addr, id=uuid.uuid4(), marketing_opt_out_at=when))
    elif status.marketing_opt_out_at is None:
        status.marketing_opt_out_at = when
    await db.flush()


async def clear_marketing_opt_out(db: AsyncSession, address: str) -> None:
    tenant_id = entity_refs.session_tenant(db)
    status = await db.get(EmailAddressStatus, (tenant_id, address.strip().lower()))
    if status is not None and status.marketing_opt_out_at is not None:
        status.marketing_opt_out_at = None
        await db.flush()


def _mail_message(
    message: Email,
    tenant_id: uuid.UUID,
    to: list[str],
    cc: list[str],
    from_name: str | None,
    extra_headers: Mapping[str, str] | None = None,
) -> mailer.EmailMessage:
    return mailer.EmailMessage(
        to=", ".join(to),
        subject=message.subject,
        text=message.body_text,
        html=message.body_html,
        tenant_id=tenant_id,
        kind=f"comms_{message.origin}",
        cc=tuple(cc),
        reply_to=message.reply_to,
        headers={**(extra_headers or {}), "X-TOS-Message-Id": str(message.id), "X-TOS-Tenant-Id": str(tenant_id)},
        from_name=from_name,
    )


async def _log_activities(
    db: AsyncSession, tenant_id: uuid.UUID, message: Email, recipients: Sequence[EmailRecipient]
) -> None:
    from app.services import activity_service

    author = (
        Principal(type="user", id=message.sender_user_id, tenant_id=tenant_id)
        if message.sender_user_id is not None
        else None
    )
    actx = RequestContext(tenant_id=tenant_id, principal=author)
    targets: list[tuple[str, uuid.UUID]] = []
    for ref in message.related or []:
        try:
            targets.append((str(ref["entity_type"]), uuid.UUID(str(ref["entity_id"]))))
        except (KeyError, ValueError):
            continue
    targets.extend((r.entity_type, r.entity_id) for r in recipients if r.entity_type and r.entity_id)
    to_addrs = [r.address for r in recipients if r.status == "sent"]
    body = f'Email sent: "{message.subject}" to {", ".join(to_addrs)}'
    for entity_type, entity_id in dict.fromkeys(targets):
        await activity_service.add_system_entry(
            actx,
            db,
            entity_type,
            entity_id,
            body,
            {"message_id": str(message.id), "transport": message.transport, "recipient_count": len(to_addrs)},
            kind="email",
        )


async def deliver(db: AsyncSession, message_id: uuid.UUID) -> str:
    """Send one queued message. Idempotent: exits unless ``queued`` / ``failed`` (row lock serialises
    concurrent runs). Returns ``sent``, ``retry``, ``failed`` or ``skipped``."""
    tenant_id = entity_refs.session_tenant(db)
    message = (
        await db.execute(select(Email).where(Email.id == message_id).with_for_update())
    ).scalar_one_or_none()
    if message is None or message.status not in ("queued", "failed"):
        return "skipped"
    recipients = list(
        (await db.execute(select(EmailRecipient).where(EmailRecipient.message_id == message.id))).scalars()
    )
    to = [r.address for r in recipients if r.kind == "to" and r.status != "suppressed"]
    cc = [r.address for r in recipients if r.kind == "cc" and r.status != "suppressed"]
    with entity_refs.acting_as(db, "system"):
        if not to:
            message.status, message.error_code = "failed", "all_recipients_suppressed"
            await db.flush()
            return "failed"
        gate = _category_gates.get(message.category)
        if gate is not None:  # Epic 39: re-checked immediately before the transport
            decision = await gate(db, message)
            if getattr(decision, "action", "send") != "send":
                message.status = "failed"
                message.error_code = f"{message.category}_{decision.action}"[:64]
                for r in recipients:
                    if r.status == "pending":
                        r.status = "suppressed"
                await db.flush()
                return "failed"
        provider_fn = _header_providers.get(message.category)
        extra_headers: Mapping[str, str] = dict(await provider_fn(db, message)) if provider_fn is not None else {}
        message.attempts = message.attempts + 1
        tenant = await entity_refs.tenant(db, tenant_id)
        sender = await entity_refs.user(db, message.sender_user_id)
        transport: str | None = None
        fallback: str | None = None
        provider_id: str | None = None
        sent_ref: Any = None  # Epic 33: mailbox SentRef (thread capture)
        send_conn: SendConnection | None = None
        try:
            if message.origin == "user" and sender is not None:
                provider = mailbox_provider()
                ctx = RequestContext(tenant_id=tenant_id)
                conn = (
                    await provider.get_send_connection(ctx, db, sender.id) if provider is not None else None
                )
                if conn is None:
                    fallback = "no_mailbox"
                else:
                    try:
                        sent_ref = await provider.send_mail(  # type: ignore[union-attr]
                            conn, _mail_message(message, tenant_id, to, cc, sender.name, extra_headers)
                        )
                        provider_id, send_conn = _sent_id(sent_ref), conn
                        transport = (
                            f"mailbox_{conn.provider}"
                            if conn.provider in ("google", "microsoft")
                            else "platform"
                        )
                    except Exception as exc:
                        if not _is_expired(exc):
                            raise
                        fallback = "token_expired"
            if transport is None:
                from_name = f"{sender.name} via {tenant.name}" if sender is not None else tenant.name
                outgoing = _mail_message(message, tenant_id, to, cc, from_name, extra_headers)
                await mailer.send_email(outgoing)
                provider_id, transport = str(outgoing.id), "platform"
                message.from_address = platform_address()
                message.fallback_reason = fallback
        except Exception as exc:
            message.error_code = type(exc).__name__[:64]
            logger.warning(
                "Email send attempt {} failed (message_id={}, tenant_id={}, error={})",
                message.attempts,
                message.id,
                tenant_id,
                message.error_code,
            )
            if message.attempts >= MAX_ATTEMPTS:
                message.status = "failed"
                await db.flush()
                emit(
                    db,
                    EmailFailed(
                        tenant_id=tenant_id,
                        message_id=message.id,
                        sender_user_id=message.sender_user_id,
                        error_code=message.error_code,
                        origin=message.origin,
                    ),
                )
                return "failed"
            message.status = "queued"
            await db.flush()
            return "retry"
        message.status, message.sent_at, message.error_code = "sent", _now(), None
        message.transport, message.provider_message_id = transport, provider_id
        for r in recipients:
            if r.status == "pending":
                r.status = "sent"
        await db.flush()
    await _log_activities(db, tenant_id, message, recipients)
    if send_conn is not None and transport.startswith("mailbox_"):
        # Epic 33 (FR-171): remember the provider thread so replies can be captured (no-op without scopes).
        from app.services.comms import inbound

        await inbound.record_thread(db, message, send_conn, sent_ref)
    emit(
        db,
        EmailSent(
            tenant_id=tenant_id,
            message_id=message.id,
            sender_user_id=message.sender_user_id,
            transport=transport,
            origin=message.origin,
        ),
    )
    return "sent"


# ================================================================ bounces
async def record_bounce(
    db: AsyncSession,
    address: str,
    provider_message_id: str | None,
    bounce_type: str,
    reason: str | None,
    *,
    message_id: uuid.UUID | None = None,
) -> BounceOut:
    """Mark the bounced recipient (matched by our message id / ``X-TOS-Message-Id``, the provider id, else
    the latest message to that address), record hard bounces per address, log an activity, emit
    ``EmailBounced``. Runs in the tenant's session."""
    tenant_id = entity_refs.session_tenant(db)
    addr = address.strip().lower()
    stmt = select(EmailRecipient).where(EmailRecipient.address == addr)
    if message_id is not None:
        stmt = stmt.where(EmailRecipient.message_id == message_id)
    elif provider_message_id:
        stmt = stmt.join(Email, Email.id == EmailRecipient.message_id).where(
            Email.provider_message_id == provider_message_id
        )
    else:
        stmt = stmt.order_by(EmailRecipient.created_at.desc()).limit(1)
    rows = list((await db.execute(stmt)).scalars())
    now = _now()
    with entity_refs.acting_as(db, "system"):
        for r in rows:
            r.status, r.bounced_at, r.bounce_type = "bounced", now, bounce_type
            r.bounce_reason = (reason or "")[:500] or None
        if bounce_type == "hard":
            status = await db.get(EmailAddressStatus, (tenant_id, addr))
            if status is None:
                db.add(
                    EmailAddressStatus(
                        tenant_id=tenant_id,
                        address=addr,
                        id=uuid.uuid4(),
                        hard_bounced_at=now,
                        last_bounce_reason=(reason or "")[:500] or None,
                    )
                )
            else:
                status.hard_bounced_at = now
                status.last_bounce_reason = (reason or "")[:500] or None
        await db.flush()
    from app.services import activity_service

    senders = (
        {
            m.id: m.sender_user_id
            for m in (
                await db.execute(select(Email).where(Email.id.in_({r.message_id for r in rows})))
            ).scalars()
        }
        if rows
        else {}
    )
    for r in rows:
        if r.entity_type and r.entity_id:
            await activity_service.add_system_entry(
                RequestContext(tenant_id=tenant_id),
                db,
                r.entity_type,
                r.entity_id,
                f"Email to {addr} bounced ({bounce_type})",
                {"message_id": str(r.message_id), "bounce_type": bounce_type},
                kind="email",
            )
        emit(
            db,
            EmailBounced(
                tenant_id=tenant_id,
                message_id=r.message_id,
                recipient_id=r.id,
                sender_user_id=senders.get(r.message_id),
                bounce_type=bounce_type,
                entity_type=r.entity_type,
                entity_id=r.entity_id,
            ),
        )
    return BounceOut(matched=bool(rows), recipient_ids=[r.id for r in rows])


def signature_for(body: bytes, secret: str | None = None) -> str:
    key = (secret if secret is not None else get_settings().email_webhook_secret).encode("utf-8")
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


async def _tenant_exists(tenant_id: uuid.UUID) -> bool:
    from sqlalchemy import column, table

    tenants = table("tenants", column("id"))
    async with platform_session() as pdb:
        return (await pdb.execute(select(tenants.c.id).where(tenants.c.id == tenant_id))).first() is not None


async def handle_bounce_webhook(raw_body: bytes, signature: str | None) -> BounceOut:
    """``POST /public/webhooks/email-bounce``: HMAC-verified generic bounce (401 on a bad signature)."""
    secret = get_settings().email_webhook_secret
    if (
        not secret
        or not signature
        or not hmac.compare_digest(signature.strip(), signature_for(raw_body, secret))
    ):
        raise Unauthorized("Invalid webhook signature", code="invalid_signature")
    try:
        data = BounceIn.model_validate(json.loads(raw_body.decode("utf-8")))
    except (ValueError, ValidationError) as exc:
        raise ValidationFailed("Invalid bounce payload", code="invalid_payload") from exc
    if not await _tenant_exists(data.tenant_id):
        return BounceOut(matched=False, recipient_ids=[])
    async with tenant_session(data.tenant_id) as db:
        return await record_bounce(
            db,
            str(data.address),
            data.provider_message_id,
            data.bounce_type,
            data.reason,
            message_id=data.message_id,
        )


async def record_bounce_everywhere(address: str, bounce_type: str = "hard", reason: str | None = None) -> int:
    """Dev CLI helper: apply a bounce in every active tenant that has sent to ``address``."""
    from app.services import directory_service as directory

    matched = 0
    for tenant_id in await directory.active_tenant_ids():
        async with tenant_session(tenant_id) as db:
            known = (
                await db.execute(
                    select(EmailRecipient.id).where(EmailRecipient.address == address.lower()).limit(1)
                )
            ).first()
            if known is None:
                continue
            result = await record_bounce(db, address, None, bounce_type, reason or "Simulated bounce (CLI)")
            matched += len(result.recipient_ids)
    return matched


# ================================================================ reads
async def _emails_out(db: AsyncSession, messages: Sequence[Email]) -> list[EmailOut]:
    ids = [m.id for m in messages]
    recipients: dict[uuid.UUID, list[EmailRecipient]] = {i: [] for i in ids}
    if ids:
        for r in (
            await db.execute(
                select(EmailRecipient)
                .where(EmailRecipient.message_id.in_(ids))
                .order_by(EmailRecipient.kind.desc())
            )
        ).scalars():
            recipients[r.message_id].append(r)
    names = await entity_refs.users(db, [m.sender_user_id for m in messages if m.sender_user_id])
    return [
        EmailOut(
            id=m.id,
            sender_user_id=m.sender_user_id,
            sender_name=names[m.sender_user_id].name if m.sender_user_id in names else None,
            transport=m.transport,
            fallback_reason=m.fallback_reason,
            from_address=m.from_address,
            reply_to=m.reply_to,
            subject=m.subject,
            status=m.status,
            category=m.category,
            origin=m.origin,
            error_code=m.error_code,
            attempts=m.attempts,
            created_at=m.created_at,
            sent_at=m.sent_at,
            related=list(m.related or []),
            recipients=[
                RecipientOut(
                    id=r.id,
                    kind=r.kind,
                    address=str(r.address),
                    entity_type=r.entity_type,
                    entity_id=r.entity_id,
                    status=r.status,
                    bounced_at=r.bounced_at,
                    bounce_type=r.bounce_type,
                )
                for r in recipients[m.id]
            ],
        )
        for m in messages
    ]


async def get_email(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> EmailDetailOut:
    """The sender, or anyone who can read a related / recipient entity."""  # ruff: ignore[non-imperative-mood]
    principal = _principal(ctx)
    message = await db.get(Email, message_id)
    if message is None:
        raise NotFound("Email not found")
    allowed = message.sender_user_id == principal.id
    if not allowed:
        targets = [(str(r.get("entity_type")), r.get("entity_id")) for r in message.related or []]
        rows = await db.execute(
            select(EmailRecipient.entity_type, EmailRecipient.entity_id).where(
                EmailRecipient.message_id == message.id, EmailRecipient.entity_id.is_not(None)
            )
        )
        targets.extend((str(t), i) for t, i in rows.all())
        for entity_type, entity_id in targets:
            try:
                eid = uuid.UUID(str(entity_id))
            except ValueError:
                continue
            if await entity_refs.can_read(ctx, db, entity_type, eid):
                allowed = True
                break
    if not allowed:
        raise NotFound("Email not found")
    await db.refresh(message)
    (out,) = await _emails_out(db, [message])
    return EmailDetailOut(**out.model_dump(), body_html=message.body_html)


async def list_entity_emails(
    ctx: RequestContext, db: AsyncSession, segment: str, entity_id: uuid.UUID, params: PageParams
) -> Page[EmailOut]:
    from app.services import activity_service

    _principal(ctx)
    entity_type = activity_service.resolve_entity_type(segment)
    await entity_refs.require_readable(ctx, db, entity_type, entity_id)
    by_recipient = select(EmailRecipient.message_id).where(
        EmailRecipient.entity_type == entity_type, EmailRecipient.entity_id == entity_id
    )
    stmt = (
        select(Email)
        .where(
            or_(
                Email.related.contains([{"entity_type": entity_type, "entity_id": str(entity_id)}]),
                Email.id.in_(by_recipient),
            )
        )
        .order_by(Email.created_at.desc(), Email.id.desc())
    )
    page = await paginate(db, stmt, params)
    return Page[EmailOut](
        items=await _emails_out(db, page.items), total=page.total, page=page.page, page_size=page.page_size
    )


# ================================================================ event wiring
def _enqueue_sends(event: EmailQueued) -> None:
    from app.tasks import comms as comms_tasks
    from app.tasks.runtime import enqueue

    for message_id in event.message_ids:
        enqueue(comms_tasks.send_email, str(event.tenant_id), str(message_id))


def _seed_templates(event: Any) -> None:
    from app.tasks import comms as comms_tasks
    from app.tasks.runtime import enqueue

    enqueue(comms_tasks.ensure_templates, str(event.tenant_id))


_registered = False


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    subscribe(EmailQueued, _enqueue_sends)
    from app.events import subscribe_optional

    subscribe_optional("app.events.identity", "TenantProvisioned", _seed_templates)
    _registered = True


register()
