"""Lead conversion (FR-84, A4; story 2.2): one transaction creating / linking the Client (``clients_service``,
status ``prospect``) and Contact, creating the Opportunity and moving the Lead to ``converted``.

The Lead's activities stay visible on the Opportunity and Client timelines through
``related_activity_refs`` (registered as an activity related-refs provider in ``hooks.py``).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.events.crm import LeadConverted
from app.models.clients import Client
from app.models.crm import Lead, Opportunity
from app.schemas.clients import ClientCreate, ContactCreate
from app.schemas.crm import LeadConvertIn, LeadConvertOut
from app.services import clients_service
from app.services.crm import leads, opportunities
from app.services.crm.common import LEAD_WRITE, OPP_WRITE, principal_of


async def convert_lead(
    ctx: RequestContext, db: AsyncSession, lead_id: uuid.UUID, data: LeadConvertIn
) -> LeadConvertOut:
    principal = principal_of(ctx, LEAD_WRITE)
    principal_of(ctx, OPP_WRITE)
    lead = await leads.get_row(ctx, db, lead_id, LEAD_WRITE, lock=True)
    if lead.status != "qualified":
        raise ConflictError(
            "Only a qualified lead can be converted",
            code="transition_not_allowed",
            details={"from": lead.status, "to": "converted"},
        )
    # ---- client
    if data.client.mode == "create":
        try:
            created = await clients_service.create_client(
                ctx,
                db,
                ClientCreate(
                    name=data.client.name,
                    website=data.client.website,
                    industry=data.client.industry,
                    gstin=data.client.gstin,
                    owner_id=data.client.owner_id or principal.id,
                    status="prospect",
                ),
            )
        except ConflictError as exc:
            if exc.code != "client_exists":
                raise
            existing = (
                await db.execute(select(Client.id).where(Client.name == data.client.name))
            ).scalar_one_or_none()
            exc.details = {**exc.details, "client_id": str(existing) if existing else None}
            raise
        client_id = created.id
    else:
        client_id = data.client.client_id
        if not await clients_service.client_access(ctx, db, client_id, False):
            raise ValidationFailed(
                "Unknown client",
                code="invalid_reference",
                details={
                    "fields": [
                        {
                            "field": "client.client_id",
                            "message": "Unknown client",
                            "type": "invalid_reference",
                        }
                    ]
                },
            )
        status = (await db.execute(select(Client.status).where(Client.id == client_id))).scalar_one()
        if status not in ("prospect", "active"):
            raise ConflictError(
                "The client must be a prospect or active", code="client_not_open", details={"status": status}
            )
    # ---- contact
    contact_id: uuid.UUID | None = None
    if data.contact.mode == "create":
        c = data.contact
        name = " ".join(p for p in (c.first_name, c.last_name) if p)
        contact = await clients_service.create_contact(
            ctx, db, client_id, ContactCreate(name=name, title=c.title, email=c.email, phone=c.phone)
        )
        contact_id = contact.id
    elif data.contact.mode == "link":
        if not await clients_service.contact_belongs_to_client(db, data.contact.contact_id, client_id):
            raise ValidationFailed(
                "The contact does not belong to the client",
                code="invalid_reference",
                details={
                    "fields": [
                        {
                            "field": "contact.contact_id",
                            "message": "Unknown contact",
                            "type": "invalid_reference",
                        }
                    ]
                },
            )
        contact_id = data.contact.contact_id
    # ---- opportunity
    opp_in = data.opportunity.model_copy(
        update={
            "client_id": client_id,
            "primary_contact_id": data.opportunity.primary_contact_id or contact_id,
        }
    )
    opp = await opportunities.create_opportunity(ctx, db, opp_in, lead_id=lead.id)
    # ---- lead -> converted
    lead.converted_opportunity_id = opp.id
    lead.converted_client_id = client_id
    lead.converted_contact_id = contact_id
    await leads.apply_transition(ctx, db, lead, "converted", actor_kind="user", via_conversion=True)
    emit(
        db,
        LeadConverted(tenant_id=lead.tenant_id, lead_id=lead.id, opportunity_id=opp.id, client_id=client_id),
    )
    return LeadConvertOut(
        lead=await leads.out(db, lead),
        opportunity=await opportunities.out(db, opp),
        client_id=client_id,
        contact_id=contact_id,
    )


async def related_activity_refs(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument] - provider signature
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
) -> list[tuple[str, uuid.UUID]]:
    """Source Leads whose activities also show on an Opportunity's / Client's timeline."""
    if entity_type == "opportunity":
        stmt = select(Opportunity.lead_id).where(
            Opportunity.id == entity_id, Opportunity.lead_id.is_not(None)
        )
    elif entity_type == "client":
        stmt = select(Lead.id).where(Lead.converted_client_id == entity_id).limit(20)
    else:
        return []
    return [("lead", i) for i in (await db.execute(stmt)).scalars() if i is not None]
