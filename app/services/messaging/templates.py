# ruff: noqa: E501
"""SMS / WhatsApp channel templates (Epic 33, FR-172/173; ``messaging:configure`` for writes).

* Bodies use the P1 ``{{ group.field }}`` merge catalogue; ``variables`` is the ordered list of merge paths a
  provider receives positionally. SMS bodies may use DLT ``{#var#}`` placeholders (one per variable, in order).
* A template is sendable only when ``registered`` with a ``provider_template_ref`` (SMS: 19-digit DLT template
  id; WhatsApp: the approved template name). Otherwise sends get 409 ``template_not_registered``.
* ``sync_whatsapp`` mirrors the provider's approved templates (``list_templates`` of the configured adapter).
"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.comms import ChannelTemplate
from app.schemas.messaging import (
    DLT_TEMPLATE_ID,
    SENDER_ID,
    ChannelTemplateIn,
    ChannelTemplateOut,
    ChannelTemplateUpdate,
    TemplateSyncOut,
)
from app.services.comms import merge_fields

SMS_MAX_CHARS = 1000
DLT_VAR = "{#var#}"
WA_PLACEHOLDER = re.compile(r"\{\{\s*(\d+)\s*\}\}")


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def sendable(t: ChannelTemplate) -> bool:
    return t.status == "registered" and bool(t.provider_template_ref)


def out(t: ChannelTemplate) -> ChannelTemplateOut:
    data = ChannelTemplateOut.model_validate(t)
    return data.model_copy(update={"sendable": sendable(t)})


def not_registered() -> ConflictError:
    return ConflictError(
        "This template is not registered with the provider and cannot be sent", code="template_not_registered"
    )


def _invalid(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(message, code="invalid_template", details={"fields": {field: message}})


def body_paths(t: ChannelTemplate | Any) -> list[str]:
    """Merge paths a template needs: the declared ``variables`` plus any ``{{ }}`` tokens of the body."""
    used = list(t.variables or [])
    if t.channel == "sms" or not WA_PLACEHOLDER.search(t.body or ""):
        used.extend(merge_fields.tokens(t.body or ""))
    return list(dict.fromkeys(used))


def validate(t: ChannelTemplate) -> None:
    """Field rules (422 ``invalid_template``) -- run on create / update."""
    if not WA_PLACEHOLDER.search(t.body or ""):
        merge_fields.validate_tokens("custom", t.body or "")
    bad = [v for v in t.variables or [] if v not in merge_fields.MERGE_FIELDS]
    if bad:
        raise ValidationFailed(
            f"Unknown merge fields: {', '.join(bad)}", code="unknown_merge_field", details={"fields": bad}
        )
    if t.channel == "sms":
        if t.provider_template_ref and not DLT_TEMPLATE_ID.match(t.provider_template_ref):
            raise _invalid("provider_template_ref", "The DLT template id has 19 digits")
        if t.sender_id and not SENDER_ID.match(t.sender_id):
            raise _invalid("sender_id", "The sender id has 6 letters or digits")
        placeholders = (t.body or "").count(DLT_VAR)
        if placeholders and placeholders != len(t.variables or []):
            raise _invalid(
                "variables", f"The body has {placeholders} {DLT_VAR} placeholders; map one variable to each"
            )
        if len(t.body or "") > SMS_MAX_CHARS:
            raise _invalid("body", f"SMS bodies are at most {SMS_MAX_CHARS} characters")
    if t.status == "registered" and not t.provider_template_ref:
        label = "DLT template id" if t.channel == "sms" else "approved WhatsApp template name"
        raise _invalid("provider_template_ref", f"A registered template needs its {label}")


def render(t: ChannelTemplate, data: merge_fields.MergeData) -> tuple[str, list[str], list[str]]:
    """``(body, positional variable values, missing paths)``. SMS ``{#var#}`` take the variable values in order;
    ``{{ group.field }}`` tokens are substituted; WhatsApp ``{{1}}`` placeholders take the values by position."""
    values: list[str] = []
    missing: list[str] = []
    for path in t.variables or []:
        field = merge_fields.MERGE_FIELDS.get(path)
        value = field.resolver(data) if field is not None else None
        if value is None or value == "":
            missing.append(path)
            value = ""
        values.append(str(value))
    body = t.body or ""
    if WA_PLACEHOLDER.search(body):
        body = WA_PLACEHOLDER.sub(
            lambda m: values[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(values) else "", body
        )
    else:
        body, token_missing = merge_fields.render(body, data, escape=False)
        missing.extend(p for p in token_missing if p not in missing)
    for value in values:
        if DLT_VAR not in body:
            break
        body = body.replace(DLT_VAR, value, 1)
    return body.strip(), values, missing


# ================================================================ CRUD
async def list_templates(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    channel: str | None = None,
    status: str | None = None,
) -> list[ChannelTemplateOut]:
    stmt = select(ChannelTemplate)
    if channel:
        stmt = stmt.where(ChannelTemplate.channel == channel)
    if status:
        stmt = stmt.where(ChannelTemplate.status == status)
    rows = (await db.execute(stmt.order_by(ChannelTemplate.channel, ChannelTemplate.name))).scalars()
    return [out(r) for r in rows]


async def get(db: AsyncSession, template_id: uuid.UUID) -> ChannelTemplate:
    row = await db.get(ChannelTemplate, template_id)
    if row is None:
        raise NotFound("Template not found")
    return row


async def create_template(
    ctx: RequestContext, db: AsyncSession, data: ChannelTemplateIn
) -> ChannelTemplateOut:
    row = ChannelTemplate(id=uuid.uuid4(), tenant_id=_tenant(db), version=1, **data.model_dump())
    validate(row)
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("A template with this key already exists", code="template_exists") from exc
    await db.refresh(row)
    return out(row)


async def update_template(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    template_id: uuid.UUID,
    data: ChannelTemplateUpdate,
) -> ChannelTemplateOut:
    row = await get(db, template_id)
    changes = data.model_dump(exclude_unset=True)
    content_changed = False
    for key, value in changes.items():
        if key in ("name", "purpose", "body", "variables", "language", "status") and value is None:
            continue
        if getattr(row, key) != value:
            content_changed = content_changed or key in (
                "body",
                "variables",
                "provider_template_ref",
                "language",
                "sender_id",
            )
            setattr(row, key, value)
    validate(row)
    if content_changed:
        row.version += 1
    await db.flush()
    await db.refresh(row)
    return out(row)


async def sync_whatsapp(ctx: RequestContext, db: AsyncSession) -> TemplateSyncOut:
    """Mirror the provider's WhatsApp templates: new approved ones are created ``registered`` (purpose
    ``other_transactional``, variables to be mapped by an admin); existing ones get body / status refreshed."""
    from app.services.connectors import registry

    adapter = await registry.get(ctx, db, "whatsapp")
    lister = getattr(adapter, "list_templates", None)
    if lister is None:
        return TemplateSyncOut(created=0, updated=0)
    provider_templates = await lister()
    existing = {
        (t.provider_template_ref, t.language): t
        for t in (
            await db.execute(select(ChannelTemplate).where(ChannelTemplate.channel == "whatsapp"))
        ).scalars()
    }
    created = updated = 0
    for pt in provider_templates:
        approved = pt.status.upper() == "APPROVED"
        current = existing.get((pt.name, pt.language))
        if current is not None:
            new_status = (
                "registered" if approved else ("draft" if current.status != "archived" else "archived")
            )
            if current.body != pt.body or current.status != new_status:
                current.body, current.status = pt.body or current.body, new_status
                current.version += 1
                updated += 1
            continue
        if not approved:
            continue
        key = re.sub(r"[^a-z0-9_.-]+", "_", f"{pt.name}.{pt.language}".lower())[:80]
        db.add(
            ChannelTemplate(
                id=uuid.uuid4(),
                tenant_id=_tenant(db),
                channel="whatsapp",
                key=key,
                name=pt.name[:200],
                purpose="other_transactional",
                body=pt.body or pt.name,
                variables=[],
                provider_template_ref=pt.name,
                language=pt.language or "en",
                status="registered",
                version=1,
            )
        )
        created += 1
    await db.flush()
    return TemplateSyncOut(created=created, updated=updated)
