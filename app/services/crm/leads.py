"""Leads (FR-83, PRD §5.1; story 2.1). ``transition_lead`` is the only writer of ``status`` (AD-4).

Transitions (actors): new <- create (user, system import); new -> contacted (user; system only via the first
logged activity); new|contacted -> qualified (user); qualified -> converted (user, only inside
``conversion.convert_lead``); new|contacted|qualified -> disqualified (user, reason code, note for ``other``);
disqualified -> new (user, reopen reason). AI / workflow actors never change a Lead Status.
"""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.crm import LeadAssigned, LeadCreated, LeadStatusChanged
from app.models.crm import Lead
from app.schemas.crm import LeadCreate, LeadOut, LeadPage, LeadTransitionIn, LeadUpdate, StatusHistoryOut
from app.services import activity_service, entity_refs
from app.services.crm import settings as crm_settings
from app.services.crm.common import (
    LEAD_READ,
    LEAD_WRITE,
    add_history,
    check_user,
    history,
    money_str,
    now,
    principal_of,
    scoped,
    tenant_of,
    transition_refused,
    user_principal,
    user_refs,
)
from app.services.crm.duplicates import find_duplicates, possible_duplicate, website_domain

SEQUENCE_KEY = "LEAD"

# (from, to) -> allowed actor kinds
TRANSITIONS: dict[tuple[str | None, str], frozenset[str]] = {
    (None, "new"): frozenset({"user", "system"}),
    ("new", "contacted"): frozenset({"user", "system"}),
    ("new", "qualified"): frozenset({"user"}),
    ("contacted", "qualified"): frozenset({"user"}),
    ("qualified", "converted"): frozenset({"user"}),
    ("new", "disqualified"): frozenset({"user"}),
    ("contacted", "disqualified"): frozenset({"user"}),
    ("qualified", "disqualified"): frozenset({"user"}),
    ("disqualified", "new"): frozenset({"user"}),
}
STATUS_LABELS = {
    "new": "New",
    "contacted": "Contacted",
    "qualified": "Qualified",
    "converted": "Converted",
    "disqualified": "Disqualified",
}


def can_transition(frm: str | None, to: str, actor_kind: str) -> bool:
    return actor_kind in TRANSITIONS.get((frm, to), frozenset())


# ---------------------------------------------------------------- read
async def _get(
    ctx: RequestContext,
    db: AsyncSession,
    lead_id: uuid.UUID,
    permission: str = LEAD_READ,
    *,
    lock: bool = False,
) -> Lead:
    principal = user_principal(ctx)
    if principal is not None:
        principal_of(ctx, permission)
    stmt = scoped(select(Lead).where(Lead.id == lead_id), principal, permission, Lead.owner_id)
    if lock:
        stmt = stmt.with_for_update()
    lead = (await db.execute(stmt)).scalar_one_or_none()
    if lead is None:
        raise NotFound("Lead not found")
    return lead  # type: ignore[no-any-return]


async def get_row(
    ctx: RequestContext,
    db: AsyncSession,
    lead_id: uuid.UUID,
    permission: str = LEAD_READ,
    *,
    lock: bool = False,
) -> Lead:
    return await _get(ctx, db, lead_id, permission, lock=lock)


async def lead_access(ctx: RequestContext, db: AsyncSession, lead_id: uuid.UUID, write: bool) -> bool:
    """Activity-registry checker (notes / tasks / timeline)."""
    principal = user_principal(ctx)
    if principal is None:
        return False
    permission = LEAD_WRITE if write else LEAD_READ
    if not principal.has(permission):
        return False
    stmt = scoped(select(Lead.id).where(Lead.id == lead_id), principal, permission, Lead.owner_id)
    return (await db.execute(stmt)).first() is not None


async def outs(db: AsyncSession, leads: list[Lead]) -> list[LeadOut]:
    users = await user_refs(db, (lead.owner_id for lead in leads))
    return [
        LeadOut(
            id=lead.id,
            human_id=lead.human_id,
            company_name=lead.company_name,
            website=lead.website,
            website_domain=lead.website_domain,
            gstin=lead.gstin,
            industry=lead.industry,
            location=lead.location,
            contact_name=lead.contact_name,
            contact_title=lead.contact_title,
            contact_email=lead.contact_email,
            contact_phone=lead.contact_phone,
            source_code=lead.source_code,
            estimated_roles=list(lead.estimated_roles or []),
            estimated_positions=lead.estimated_positions,
            expected_annual_revenue=money_str(lead.expected_annual_revenue),
            currency=lead.currency,
            owner_id=lead.owner_id,
            owner=users.get(lead.owner_id),
            status=lead.status,
            disqualify_reason_code=lead.disqualify_reason_code,
            disqualify_note=lead.disqualify_note,
            converted_at=lead.converted_at,
            converted_opportunity_id=lead.converted_opportunity_id,
            converted_client_id=lead.converted_client_id,
            converted_contact_id=lead.converted_contact_id,
            archived_at=lead.archived_at,
            created_at=lead.created_at,
            updated_at=lead.updated_at,
        )
        for lead in leads
    ]


async def out(db: AsyncSession, lead: Lead) -> LeadOut:
    await db.refresh(lead)
    return (await outs(db, [lead]))[0]


async def get_lead(ctx: RequestContext, db: AsyncSession, lead_id: uuid.UUID) -> LeadOut:
    return await out(db, await _get(ctx, db, lead_id))


async def _tz(db: AsyncSession) -> ZoneInfo:
    return (await entity_refs.tenant(db, tenant_of(db))).tz


async def list_leads(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: list[str] | None = None,
    owner_id: uuid.UUID | None = None,
    source: str | None = None,
    q: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    include_archived: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> LeadPage:
    principal = principal_of(ctx, LEAD_READ)
    stmt = scoped(select(Lead), principal, LEAD_READ, Lead.owner_id)
    if status:
        stmt = stmt.where(Lead.status.in_(status))
    if owner_id:
        stmt = stmt.where(Lead.owner_id == owner_id)
    if source:
        stmt = stmt.where(Lead.source_code == source)
    if q:
        term = f"%{q.strip().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(
            or_(Lead.company_name.ilike(term), Lead.human_id.ilike(term), Lead.contact_name.ilike(term))
        )
    if created_from or created_to:
        tz = await _tz(db)
        if created_from:
            stmt = stmt.where(Lead.created_at >= datetime.combine(created_from, time.min, tzinfo=tz))
        if created_to:
            stmt = stmt.where(
                Lead.created_at < datetime.combine(created_to + timedelta(days=1), time.min, tzinfo=tz)
            )
    if not include_archived:
        stmt = stmt.where(Lead.archived_at.is_(None))
    page, page_size = max(1, page), max(1, min(page_size, 200))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = await db.execute(
        stmt.order_by(Lead.created_at.desc(), Lead.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return LeadPage(items=await outs(db, list(rows.scalars())), total=total, page=page, page_size=page_size)


async def lead_history(ctx: RequestContext, db: AsyncSession, lead_id: uuid.UUID) -> list[StatusHistoryOut]:
    await _get(ctx, db, lead_id)
    return [StatusHistoryOut.model_validate(h) for h in await history(db, "lead", lead_id)]


# ---------------------------------------------------------------- write
def _apply_fields(lead: Lead, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if key == "estimated_roles":
            value = [r if isinstance(r, dict) else r.model_dump() for r in (value or [])]
        setattr(lead, key, value)
    if "website" in values:
        lead.website_domain = website_domain(lead.website)


async def create_lead(
    ctx: RequestContext,
    db: AsyncSession,
    data: LeadCreate,
    *,
    actor_kind: str = "user",
    import_batch_id: uuid.UUID | None = None,
    owner_default: uuid.UUID | None = None,
    check_duplicates: bool = True,
) -> Lead:
    principal = user_principal(ctx)
    if actor_kind == "user":
        principal = principal_of(ctx, LEAD_WRITE)
    if not can_transition(None, "new", actor_kind):
        raise transition_refused("lead", None, "new")
    owner_id = data.owner_id or owner_default or (principal.id if principal else None)
    if owner_id is None:
        raise NotFound("Owner required")
    if principal is None or owner_id != principal.id:
        await check_user(db, owner_id, "owner_id")
    await crm_settings.require_source(db, data.source_code)
    if check_duplicates:
        matches = await find_duplicates(ctx, db, data.company_name, data.website, data.gstin)
        if matches and not data.acknowledge_duplicates:
            raise possible_duplicate(matches)
    values = data.model_dump(exclude={"acknowledge_duplicates", "owner_id"}, exclude_none=True)
    lead = Lead(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, SEQUENCE_KEY, prefix="LEAD", width=6),
        owner_id=owner_id,
        status="new",
        currency=data.currency or await crm_settings.default_currency(db),
        import_batch_id=import_batch_id,
        estimated_roles=[],
    )
    _apply_fields(lead, values)
    if check_duplicates and data.acknowledge_duplicates and principal is not None:
        lead.duplicate_ack_by = principal.id
    actor_id = principal.id if principal else None
    with entity_refs.acting_as(db, actor_kind, actor_id):
        db.add(lead)
        await db.flush()
    if lead.duplicate_ack_by is not None:
        audit.record(
            "lead.duplicate_acknowledged", "lead", lead.id, session=db, by=str(lead.duplicate_ack_by)
        )
    add_history(
        db,
        entity_kind="lead",
        entity_id=lead.id,
        frm=None,
        to="new",
        actor_kind=actor_kind,
        actor_id=actor_id,
    )
    if import_batch_id is None:
        await activity_service.add_system_entry(ctx, db, "lead", lead.id, f"Lead {lead.human_id} created")
    emit(
        db,
        LeadCreated(tenant_id=lead.tenant_id, lead_id=lead.id, owner_id=lead.owner_id, actor_kind=actor_kind),
    )
    if principal is not None and owner_id != principal.id:
        emit(
            db,
            LeadAssigned(tenant_id=lead.tenant_id, lead_id=lead.id, owner_id=owner_id, actor_id=principal.id),
        )
    return lead


async def create(ctx: RequestContext, db: AsyncSession, data: LeadCreate) -> LeadOut:
    return await out(db, await create_lead(ctx, db, data))


def _not_converted(lead: Lead) -> None:
    if lead.status == "converted":
        raise ConflictError("A converted lead is read-only", code="lead_converted")


async def update_lead(ctx: RequestContext, db: AsyncSession, lead_id: uuid.UUID, data: LeadUpdate) -> LeadOut:
    principal = principal_of(ctx, LEAD_WRITE)
    lead = await _get(ctx, db, lead_id, LEAD_WRITE, lock=True)
    _not_converted(lead)
    values = data.model_dump(exclude_unset=True, exclude={"acknowledge_duplicates"})
    if "company_name" in values and values["company_name"] is None:
        values.pop("company_name")
    if "source_code" in values:
        await crm_settings.require_source(db, values["source_code"])
    if "currency" in values and values["currency"] is None:
        values.pop("currency")
    owner_changed = False
    if values.get("owner_id") and values["owner_id"] != lead.owner_id:
        await check_user(db, values["owner_id"], "owner_id")
        owner_changed = True
    elif "owner_id" in values:
        values.pop("owner_id")
    identity_changed = any(
        k in values and values[k] != getattr(lead, k) for k in ("company_name", "website", "gstin")
    )
    if identity_changed:
        matches = await find_duplicates(
            ctx,
            db,
            values.get("company_name", lead.company_name),
            values.get("website", lead.website),
            values.get("gstin", lead.gstin),
            exclude_lead_id=lead.id,
        )
        if matches and not data.acknowledge_duplicates:
            raise possible_duplicate(matches)
        if matches:
            lead.duplicate_ack_by = principal.id
            audit.record("lead.duplicate_acknowledged", "lead", lead.id, session=db, by=str(principal.id))
    _apply_fields(lead, values)
    await db.flush()
    if owner_changed:
        emit(
            db,
            LeadAssigned(
                tenant_id=lead.tenant_id, lead_id=lead.id, owner_id=lead.owner_id, actor_id=principal.id
            ),
        )
    return await out(db, lead)


async def archive_lead(ctx: RequestContext, db: AsyncSession, lead_id: uuid.UUID) -> LeadOut:
    principal_of(ctx, LEAD_WRITE)
    lead = await _get(ctx, db, lead_id, LEAD_WRITE, lock=True)
    _not_converted(lead)
    if lead.archived_at is None:
        lead.archived_at = now()
        await db.flush()
    return await out(db, lead)


async def apply_transition(
    ctx: RequestContext,
    db: AsyncSession,
    lead: Lead,
    to: str,
    *,
    actor_kind: str,
    reason_code: str | None = None,
    note: str | None = None,
    via_conversion: bool = False,
) -> None:
    """Validate and apply one transition (history, system activity, audit, event)."""
    frm = lead.status
    if not can_transition(frm, to, actor_kind) or (to == "converted" and not via_conversion):
        raise transition_refused("lead", frm, to)
    if to == "disqualified":
        reason_code = await crm_settings.require_reason(db, "lead_disqualify", reason_code, note)
        lead.disqualify_reason_code = reason_code
        lead.disqualify_note = note
    elif to == "new" and frm == "disqualified":
        reason_code = await crm_settings.require_reason(db, "reopen", reason_code, note)
        lead.disqualify_reason_code = None
        lead.disqualify_note = None
    principal = user_principal(ctx)
    actor_id = principal.id if principal and actor_kind == "user" else None
    with entity_refs.acting_as(db, actor_kind, actor_id):
        lead.status = to
        if to == "converted":
            lead.converted_at = now()
        await db.flush()
    add_history(
        db, entity_kind="lead", entity_id=lead.id, frm=frm, to=to, actor_kind=actor_kind, actor_id=actor_id,
        reason_code=reason_code, note=note,
    )  # fmt: skip
    await activity_service.add_system_entry(
        ctx, db, "lead", lead.id, f"Status changed: {STATUS_LABELS[frm]} -> {STATUS_LABELS[to]}",
        {"from": frm, "to": to, "reason_code": reason_code, "actor_kind": actor_kind},
    )  # fmt: skip
    emit(db, LeadStatusChanged(tenant_id=lead.tenant_id, lead_id=lead.id, from_status=frm, to_status=to))


async def transition_lead(
    ctx: RequestContext,
    db: AsyncSession,
    lead_id: uuid.UUID,
    data: LeadTransitionIn,
    *,
    actor_kind: str = "user",
) -> LeadOut:
    if actor_kind == "user":
        principal_of(ctx, LEAD_WRITE)
    lead = await _get(ctx, db, lead_id, LEAD_WRITE, lock=True)
    if lead.archived_at is not None:
        raise ConflictError("An archived lead cannot change status", code="lead_archived")
    if lead.status == "converted":
        raise ConflictError("A converted lead is read-only", code="lead_converted")
    await apply_transition(
        ctx, db, lead, data.to, actor_kind=actor_kind, reason_code=data.reason_code, note=data.note
    )
    return await out(db, lead)


async def mark_contacted(ctx: RequestContext, db: AsyncSession, lead_id: uuid.UUID) -> bool:
    """System New -> Contacted on the first logged activity (idempotent; no-op for any other status)."""
    lead = (await db.execute(select(Lead).where(Lead.id == lead_id).with_for_update())).scalar_one_or_none()
    if lead is None or lead.status != "new" or lead.archived_at is not None:
        return False
    await apply_transition(ctx, db, lead, "contacted", actor_kind="system")
    return True
