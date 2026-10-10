# ruff: noqa: E501
"""Agent email templates (Epic 62 A5): "Agent · Document reminder", "Agent · Credential expiry reminder" and
"Agent · BGV status request" -- English, ``key='custom'``, ``is_system=true``, editable in the P1 template editor.
Seeded idempotently on first use (``ensure_system_template``); the tenant's edited version is used when present.

Agent-specific placeholders (``{{ reminder.items }}``, ``{{ reminder.deadline }}``, ``{{ bgv.case_ref }}`` ...) are
filled here from records before the P1 merge renderer fills the standard ones (``{{ candidate.first_name }}``).
"""

import html
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comms import EmailTemplate, EmailTemplateVariant
from app.services import entity_refs

DOCUMENT_REMINDER = "Agent · Document reminder"
CREDENTIAL_REMINDER = "Agent · Credential expiry reminder"
BGV_STATUS_REQUEST = "Agent · BGV status request"

DEFAULTS: dict[str, tuple[str, str, str]] = {
    DOCUMENT_REMINDER: (
        "Reminder from the Compliance Agent: documents needed before your start date.",
        "Documents needed before {{ reminder.deadline }}",
        "<p>Hi {{ candidate.first_name }},</p>"
        "<p>To start on time, please upload the following in your Worker portal before {{ reminder.deadline }}:</p>"
        "{{ reminder.items }}"
        "<p>Open <strong>My Work &rarr; Onboarding</strong> to upload them. If you have already uploaded them, you can ignore this message.</p>",
    ),
    CREDENTIAL_REMINDER: (
        "Reminder from the Compliance Agent: a credential or permit is about to expire.",
        "Your document expires on {{ reminder.deadline }}",
        "<p>Hi {{ candidate.first_name }},</p>"
        "<p>The following document on your record expires on {{ reminder.deadline }}:</p>"
        "{{ reminder.items }}"
        "<p>Please upload the renewed document in your Worker portal (<strong>My Work &rarr; Credentials</strong>).</p>",
    ),
    BGV_STATUS_REQUEST: (
        "Status request to a BGV provider about an overdue check (no documents, links or identity numbers).",
        "Status request: {{ bgv.case_ref }} -- {{ bgv.check_type }}",
        "<p>Hello,</p>"
        "<p>Could you please share the current status of this background verification check?</p>"
        "<ul><li>Case reference: {{ bgv.case_ref }}</li><li>Check: {{ bgv.check_type }} ({{ bgv.subject_label }})</li>"
        "<li>Your reference: {{ bgv.external_ref }}</li><li>Started: {{ bgv.started_on }}</li><li>Due: {{ bgv.due_on }}</li>"
        "<li>Days overdue: {{ bgv.days_overdue }}</li></ul>"
        "<p>Please reply to this email with the status and expected completion date.</p>",
    ),
}
_TOKEN = re.compile(r"\{\{\s*(reminder|bgv)\.([a-z_]+)\s*\}\}")


@dataclass(frozen=True, slots=True)
class Rendered:
    template_id: uuid.UUID
    variant_id: uuid.UUID
    version: int
    subject: str
    body_html: str


async def ensure_system_template(db: AsyncSession, name: str) -> tuple[EmailTemplate, EmailTemplateVariant]:
    """Idempotently seed (or load) a ``custom`` system template by name; returns the template and its ``en`` variant."""
    description, subject, body = DEFAULTS[name]
    template = (
        await db.execute(
            select(EmailTemplate)
            .where(
                EmailTemplate.key == "custom", EmailTemplate.name == name, EmailTemplate.archived_at.is_(None)
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if template is None:
        from app.services.comms.html import sanitize_html

        tenant_id = entity_refs.session_tenant(db)
        try:
            async with db.begin_nested():
                with entity_refs.acting_as(db, "system"):
                    template = EmailTemplate(
                        id=uuid.uuid4(), tenant_id=tenant_id, key="custom", name=name, description=description,
                        is_system=True,
                    )  # fmt: skip
                    db.add(template)
                    await db.flush()
                    db.add(
                        EmailTemplateVariant(
                            id=uuid.uuid4(), tenant_id=tenant_id, template_id=template.id, language="en",
                            subject=subject, body_html=sanitize_html(body), version=1,
                        )
                    )  # fmt: skip
                    await db.flush()
        except (IntegrityError, DBAPIError):
            template = (
                await db.execute(
                    select(EmailTemplate)
                    .where(EmailTemplate.key == "custom", EmailTemplate.name == name)
                    .limit(1)
                )
            ).scalar_one()
    variant = (
        (
            await db.execute(
                select(EmailTemplateVariant)
                .where(EmailTemplateVariant.template_id == template.id)
                .order_by(EmailTemplateVariant.language)
            )
        )
        .scalars()
        .first()
    )
    if variant is None:  # pragma: no cover - a template without a variant (edited away)
        variant = EmailTemplateVariant(
            id=uuid.uuid4(), tenant_id=template.tenant_id, template_id=template.id, language="en", subject=subject,
            body_html=body, version=1,
        )  # fmt: skip
        db.add(variant)
        await db.flush()
    return template, variant


async def ensure_all(db: AsyncSession) -> None:
    for name in DEFAULTS:
        await ensure_system_template(db, name)


def fill(text: str, values: Mapping[str, str]) -> str:
    """Fill the agent placeholders; values are HTML-escaped unless the key ends with ``_html``."""

    def repl(m: re.Match[str]) -> str:
        key = f"{m.group(1)}.{m.group(2)}"
        if f"{key}_html" in values:
            return values[f"{key}_html"]
        return html.escape(values.get(key, ""))

    return _TOKEN.sub(repl, text)


async def render(db: AsyncSession, name: str, values: Mapping[str, str]) -> Rendered:
    template, variant = await ensure_system_template(db, name)
    return Rendered(
        template_id=template.id, variant_id=variant.id, version=variant.version,
        subject=fill(variant.subject, values), body_html=fill(variant.body_html, values),
    )  # fmt: skip


def items_html(labels: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(label)}</li>" for label in labels) + "</ul>"


__all__ = [
    "BGV_STATUS_REQUEST",
    "CREDENTIAL_REMINDER",
    "DEFAULTS",
    "DOCUMENT_REMINDER",
    "Rendered",
    "ensure_all",
    "ensure_system_template",
    "fill",
    "items_html",
    "render",
]
