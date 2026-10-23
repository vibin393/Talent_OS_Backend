# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Collection rules CRUD (Epic 63 A4, FR-314). Per-client rules need ``collections:write``; the tenant default needs
``billing:configure``. Every change is audited (``AuditedMixin``). The escalation Contact must belong to the Client
(422 ``escalation_contact_other_client``); only a templated pre-due courteous / friendly step to billing contacts may
``allow_auto`` (schema validator + CHECK ``auto``).

``ensure_defaults`` seeds, once per tenant, the three system templates ("Agent · Payment reminder (courteous /
friendly / firm)", key ``custom``) and the default rule "Standard reminders" (-3 courteous, +7 friendly, +21 firm, +30
escalation to the escalation Contact; nothing automatic). ``effective_rule(client)`` = the Client's rule, else the
default, else none.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.models.clients import Client, Contact
from app.models.collection_rules import CollectionRule, CollectionRuleStep
from app.schemas.collection_rules import RuleIn, RuleOut, RuleUpdate, StepIn, StepOut

DEFAULT_NAME = "Standard reminders"
TEMPLATE_NAMES = {
    "courteous": "Agent · Payment reminder (courteous)",
    "friendly": "Agent · Payment reminder (friendly)",
    "firm": "Agent · Payment reminder (firm)",
}
TEMPLATE_VARIABLES = (
    "client_name", "contact_name", "invoices_table", "total_remindable", "currency", "oldest_due_date",
    "collections_owner_name", "tenant_name",
)  # fmt: skip
_OPENERS = {
    "courteous": (
        "Friendly reminder: invoices due soon",
        "<p>Dear {{contact_name}},</p><p>This is a courteous reminder that the invoices below for {{client_name}} fall due soon. "
        "Nothing is needed if payment is already scheduled.</p>",
    ),
    "friendly": (
        "Payment reminder: invoices now due",
        "<p>Dear {{contact_name}},</p><p>Our records show the invoices below for {{client_name}} are now past their due date. "
        "Could you let us know when payment is planned? If anything needs clarifying, just reply.</p>",
    ),
    "firm": (
        "Overdue invoices: please confirm a payment date",
        "<p>Dear {{contact_name}},</p><p>The invoices below for {{client_name}} remain unpaid past their due date. "
        "Please confirm a payment date, or tell us about any query so we can resolve it quickly.</p>",
    ),
}
_CLOSING = (
    "{{invoices_table}}<p>Total outstanding: {{currency}} {{total_remindable}} (oldest due {{oldest_due_date}}).</p>"
    "<p>Regards,<br>{{collections_owner_name}}<br>{{tenant_name}}</p>"
)
DEFAULT_STEPS: tuple[tuple[int, str, str], ...] = (
    (-3, "courteous", "billing_contacts"),
    (7, "friendly", "billing_contacts"),
    (21, "firm", "billing_contacts"),
    (30, "escalation", "escalation_contact"),
)


def _uid(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return getattr(p, "id", None) if p is not None and getattr(p, "type", "") == "user" else None


def _require(ctx: RequestContext, client_id: uuid.UUID | None) -> None:
    perm = "billing:configure" if client_id is None else "collections:write"
    p = ctx.principal
    if p is None or not p.has(perm):
        raise Forbidden("You do not have permission to change this collection rule", details={"permission": perm})


async def _out(db: AsyncSession, rule: CollectionRule) -> RuleOut:
    steps = list(
        (
            await db.execute(
                select(CollectionRuleStep).where(CollectionRuleStep.rule_id == rule.id).order_by(CollectionRuleStep.position)
            )
        ).scalars()
    )
    client_name = (await db.execute(select(Client.name).where(Client.id == rule.client_id))).scalar() if rule.client_id else None
    contact_name = (
        (await db.execute(select(Contact.name).where(Contact.id == rule.escalation_contact_id))).scalar()
        if rule.escalation_contact_id
        else None
    )
    return RuleOut(
        id=rule.id, client_id=rule.client_id, client_name=str(client_name) if client_name else None, name=rule.name,
        active=rule.active, escalation_contact_id=rule.escalation_contact_id, escalation_contact_name=contact_name,
        max_reminders_per_week=rule.max_reminders_per_week, quiet_start=rule.quiet_start, quiet_end=rule.quiet_end,
        timezone=rule.timezone, steps=[StepOut.model_validate(s) for s in steps],
        auto_steps=sum(1 for s in steps if s.allow_auto and s.active), updated_at=rule.updated_at,
    )  # fmt: skip


async def _check_contact(db: AsyncSession, client_id: uuid.UUID | None, contact_id: uuid.UUID | None) -> None:
    if contact_id is None:
        return
    contact = await db.get(Contact, contact_id)
    if contact is None:
        raise ValidationFailed("The escalation contact was not found", code="escalation_contact_not_found")
    if client_id is None or contact.client_id != client_id:
        raise ValidationFailed(
            "The escalation contact must be a contact of this client", code="escalation_contact_other_client"
        )


async def _check_templates(db: AsyncSession, steps: list[StepIn]) -> None:
    from app.models.comms import EmailTemplate

    ids = {s.email_template_id for s in steps if s.email_template_id is not None}
    if not ids:
        return
    found = set((await db.execute(select(EmailTemplate.id).where(EmailTemplate.id.in_(ids)))).scalars())
    if ids - found:
        raise ValidationFailed("An email template was not found", code="template_not_found")


async def _replace_steps(db: AsyncSession, rule: CollectionRule, steps: list[StepIn]) -> None:
    positions = [s.position for s in steps]
    if len(set(positions)) != len(positions):
        raise ValidationFailed("Step positions must be unique", code="duplicate_position")
    await _check_templates(db, steps)
    await db.execute(delete(CollectionRuleStep).where(CollectionRuleStep.rule_id == rule.id))
    await db.flush()
    for s in steps:
        db.add(CollectionRuleStep(id=uuid.uuid4(), tenant_id=rule.tenant_id, rule_id=rule.id, **s.model_dump()))
    await db.flush()


async def _get(db: AsyncSession, rule_id: uuid.UUID, *, lock: bool = False) -> CollectionRule:
    stmt = select(CollectionRule).where(CollectionRule.id == rule_id)
    if lock:
        stmt = stmt.with_for_update()
    rule = (await db.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise NotFound("Collection rule not found")
    return rule


async def list_rules(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID | None = None) -> list[RuleOut]:  # ruff: ignore[unused-function-argument]
    stmt = select(CollectionRule)
    if client_id is not None:
        stmt = stmt.where(CollectionRule.client_id == client_id)
    rows = (await db.execute(stmt.order_by(CollectionRule.client_id.is_not(None), CollectionRule.name))).scalars()
    return [await _out(db, r) for r in rows]


async def get_rule(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID) -> RuleOut:  # ruff: ignore[unused-function-argument]
    return await _out(db, await _get(db, rule_id))


async def create_rule(ctx: RequestContext, db: AsyncSession, data: RuleIn) -> RuleOut:
    _require(ctx, data.client_id)
    if data.client_id is not None and await db.get(Client, data.client_id) is None:
        raise NotFound("Client not found")
    await _check_contact(db, data.client_id, data.escalation_contact_id)
    values = data.model_dump(exclude={"steps"})
    rule = CollectionRule(id=uuid.uuid4(), tenant_id=_tenant(db), updated_by=_uid(ctx), **values)
    try:
        async with db.begin_nested():
            db.add(rule)
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("This client already has a collection rule", code="rule_exists") from exc
    steps = data.steps if data.steps is not None else await _default_steps(db)
    await _replace_steps(db, rule, steps)
    return await _out(db, rule)


async def update_rule(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID, data: RuleUpdate) -> RuleOut:
    rule = await _get(db, rule_id, lock=True)
    _require(ctx, rule.client_id)
    values = data.model_dump(exclude_unset=True)
    if "escalation_contact_id" in values:
        await _check_contact(db, rule.client_id, values["escalation_contact_id"])
    for k, v in values.items():
        setattr(rule, k, v)
    rule.updated_by = _uid(ctx)
    rule.updated_at = datetime.now(UTC)
    await db.flush()
    return await _out(db, rule)


async def put_steps(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID, steps: list[StepIn]) -> RuleOut:
    rule = await _get(db, rule_id, lock=True)
    _require(ctx, rule.client_id)
    await _replace_steps(db, rule, steps)
    rule.updated_by = _uid(ctx)
    rule.updated_at = datetime.now(UTC)
    await db.flush()
    return await _out(db, rule)


async def delete_rule(ctx: RequestContext, db: AsyncSession, rule_id: uuid.UUID) -> None:
    rule = await _get(db, rule_id, lock=True)
    _require(ctx, rule.client_id)
    await db.delete(rule)
    await db.flush()


def _tenant(db: AsyncSession) -> uuid.UUID:
    from app.services import entity_refs

    return entity_refs.session_tenant(db)


# ---------------------------------------------------------------- defaults
async def ensure_templates(db: AsyncSession) -> dict[str, uuid.UUID]:
    from app.models.comms import EmailTemplate, EmailTemplateVariant

    tid = _tenant(db)
    out: dict[str, uuid.UUID] = {}
    for tone, name in TEMPLATE_NAMES.items():
        existing = (
            await db.execute(select(EmailTemplate.id).where(EmailTemplate.key == "custom", EmailTemplate.name == name))
        ).scalar_one_or_none()
        if existing is not None:
            out[tone] = existing
            continue
        tpl = EmailTemplate(
            id=uuid.uuid4(), tenant_id=tid, key="custom", name=name, is_system=True,
            description="Collections Agent reminder. Variables: " + ", ".join(TEMPLATE_VARIABLES),
        )  # fmt: skip
        db.add(tpl)
        await db.flush()
        subject, opener = _OPENERS[tone]
        db.add(
            EmailTemplateVariant(
                id=uuid.uuid4(), tenant_id=tid, template_id=tpl.id, language="en", subject=subject, body_html=opener + _CLOSING
            )
        )
        await db.flush()
        out[tone] = tpl.id
    return out


async def _default_steps(db: AsyncSession) -> list[StepIn]:
    templates = await ensure_templates(db)
    return [
        StepIn(
            position=i, offset_days=offset, tone=tone, audience=audience,
            email_template_id=templates.get(tone), allow_auto=False, create_task=tone == "escalation",
        )
        for i, (offset, tone, audience) in enumerate(DEFAULT_STEPS, 1)
    ]  # fmt: skip


async def ensure_defaults(db: AsyncSession) -> uuid.UUID:
    """The tenant default rule (seeded on first enable / first read)."""
    existing = (
        await db.execute(select(CollectionRule).where(CollectionRule.client_id.is_(None)))
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id
    rule = CollectionRule(id=uuid.uuid4(), tenant_id=_tenant(db), client_id=None, name=DEFAULT_NAME)
    try:
        async with db.begin_nested():
            db.add(rule)
            await db.flush()
    except IntegrityError:
        found = (await db.execute(select(CollectionRule.id).where(CollectionRule.client_id.is_(None)))).scalar_one()
        return uuid.UUID(str(found))
    await _replace_steps(db, rule, await _default_steps(db))
    return rule.id


async def effective_rule(db: AsyncSession, client_id: uuid.UUID) -> tuple[CollectionRule | None, list[CollectionRuleStep]]:
    rule = (
        await db.execute(select(CollectionRule).where(CollectionRule.client_id == client_id))
    ).scalar_one_or_none()
    if rule is None:
        rule = (await db.execute(select(CollectionRule).where(CollectionRule.client_id.is_(None)))).scalar_one_or_none()
    if rule is None:
        return None, []
    steps = list(
        (
            await db.execute(
                select(CollectionRuleStep).where(CollectionRuleStep.rule_id == rule.id).order_by(CollectionRuleStep.position)
            )
        ).scalars()
    )
    return rule, steps


async def template_variant(db: AsyncSession, template_id: uuid.UUID | None) -> Any:
    from app.models.comms import EmailTemplateVariant

    if template_id is None:
        return None
    return (
        await db.execute(select(EmailTemplateVariant).where(EmailTemplateVariant.template_id == template_id).limit(1))
    ).scalar_one_or_none()


__all__ = [
    "DEFAULT_NAME",
    "DEFAULT_STEPS",
    "TEMPLATE_NAMES",
    "create_rule",
    "delete_rule",
    "effective_rule",
    "ensure_defaults",
    "ensure_templates",
    "get_rule",
    "list_rules",
    "put_steps",
    "template_variant",
    "update_rule",
]
