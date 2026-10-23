# ruff: file-ignore[line-too-long]
"""Registrations of the ``crm`` domain (autodiscovered by ``core.hooks``; Epic 15 A8).

* Activity entities ``lead`` / ``opportunity`` (notes, logged calls / meetings, emails, tasks, timelines) and the
  related-refs provider (a source Lead's activities show on its Opportunity's and Client's timelines).
* ``ActivityLogged`` (call / meeting / email on a New Lead) -> ``crm.mark_contacted`` (system New -> Contacted).
* Workflow triggers ``crm.lead_created``, ``crm.lead_status_changed``, ``crm.lead_converted``,
  ``crm.opportunity_stage_changed``, ``crm.opportunity_won``, ``crm.opportunity_lost``. Workflow actions may
  assign owners or create tasks; Lead Status / Opportunity Stage actor sets exclude ``workflow`` and ``ai``.
* Notification types ``crm.opportunity_won_handoff`` (Account Manager), ``crm.lead_assigned``,
  ``crm.opportunity_assigned`` (new owner).
* Lead import: enqueue ``crm.import_leads`` after the batch row commits.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.events.activity import ActivityLogged
from app.services import activity_service, files_service
from app.services.crm import conversion, imports, leads, opportunities
from app.services.notifications_service import (
    Content,
    NotificationType,
    register_event_source,
    register_notification_type,
)
from app.services.workflow.registry import Trigger, register_trigger

CONTACT_KINDS = frozenset({"call", "meeting", "email"})

# ---------------------------------------------------------------- activity entities + related refs
activity_service.register_entity("lead", leads.lead_access, plural="leads")
activity_service.register_entity("opportunity", opportunities.opportunity_access, plural="opportunities")
activity_service.register_related_refs_provider("crm.source_lead", conversion.related_activity_refs)
# Attachments (e.g. the contract PDF uploaded in the Won dialog) follow the record's access.
files_service.register_access_checker("lead", leads.lead_access, plural="leads")
files_service.register_access_checker("opportunity", opportunities.opportunity_access, plural="opportunities")


# ---------------------------------------------------------------- after-commit handlers
def _on_activity_logged(event: ActivityLogged) -> None:
    if event.entity_type != "lead" or event.kind not in CONTACT_KINDS:
        return
    from app.tasks.crm import mark_contacted
    from app.tasks.runtime import enqueue

    enqueue(mark_contacted, str(event.tenant_id), str(event.entity_id))


def _on_import_queued(event: imports.LeadImportQueued) -> None:
    from app.tasks.crm import import_leads
    from app.tasks.runtime import enqueue

    enqueue(import_leads, str(event.tenant_id), str(event.batch_id))


subscribe(ActivityLogged, _on_activity_logged)
subscribe(imports.LeadImportQueued, _on_import_queued)


# ---------------------------------------------------------------- workflow triggers
_MODULE = "app.events.crm"
for _trigger in (
    Trigger("crm.lead_created", "Lead created", _MODULE, "LeadCreated", "lead", "lead_id"),
    Trigger(
        "crm.lead_status_changed", "Lead status changed", _MODULE, "LeadStatusChanged", "lead", "lead_id"
    ),
    Trigger("crm.lead_converted", "Lead converted", _MODULE, "LeadConverted", "lead", "lead_id"),
    Trigger(
        "crm.opportunity_stage_changed",
        "Opportunity stage changed",
        _MODULE,
        "OpportunityStageChanged",
        "opportunity",
        "opportunity_id",
    ),
    Trigger(
        "crm.opportunity_won", "Opportunity won", _MODULE, "OpportunityWon", "opportunity", "opportunity_id"
    ),
    Trigger(
        "crm.opportunity_lost",
        "Opportunity lost",
        _MODULE,
        "OpportunityLost",
        "opportunity",
        "opportunity_id",
    ),
):
    try:
        register_trigger(_trigger.key, _trigger)
    except Exception:
        logger.debug("trigger {} already registered", _trigger.key)


# ---------------------------------------------------------------- notifications
def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _pick(key: str) -> Any:
    async def resolve(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        value = _uuid(payload.get(key))
        return {value} if value else set()

    return resolve


async def _opp_label(db: AsyncSession, payload: Any) -> tuple[uuid.UUID | None, str]:
    from app.models.crm import Opportunity

    opp_id = _uuid(payload.get("opportunity_id"))
    row = None
    if opp_id is not None:
        row = (
            await db.execute(select(Opportunity.human_id, Opportunity.name).where(Opportunity.id == opp_id))
        ).first()
    return opp_id, (f"{row[0]} {row[1]}" if row else "An opportunity")


async def _render_handoff(db: AsyncSession, payload: Any) -> Content:
    opp_id, label = await _opp_label(db, payload)
    return Content(
        title=f"{label} was won -- you are the Account Manager"[:300],
        body="Review the activated client, the draft contract, rate card and demands created by the conversion.",
        entity_type="opportunity",
        entity_id=opp_id,
        link_path=f"/opportunities/{opp_id}" if opp_id else None,
    )


async def _render_opp_assigned(db: AsyncSession, payload: Any) -> Content:
    opp_id, label = await _opp_label(db, payload)
    return Content(
        title=f"{label} was assigned to you"[:300],
        entity_type="opportunity",
        entity_id=opp_id,
        link_path=f"/opportunities/{opp_id}" if opp_id else None,
    )


async def _render_lead_assigned(db: AsyncSession, payload: Any) -> Content:
    from app.models.crm import Lead

    lead_id = _uuid(payload.get("lead_id"))
    row = None
    if lead_id is not None:
        row = (await db.execute(select(Lead.human_id, Lead.company_name).where(Lead.id == lead_id))).first()
    return Content(
        title=f"Lead {row[0]} {row[1]} was assigned to you"[:300] if row else "A lead was assigned to you",
        entity_type="lead",
        entity_id=lead_id,
        link_path=f"/leads/{lead_id}" if lead_id else None,
    )


def _register_notifications() -> None:
    from app.events.crm import LeadAssigned, OpportunityAssigned, OpportunityWonHandoff

    types = {
        "crm.opportunity_won_handoff": (
            NotificationType(
                "Opportunity won: hand-off",
                "Sales",
                "both",
                True,
                _pick("account_manager_id"),
                _render_handoff,
            ),
            OpportunityWonHandoff,
        ),
        "crm.lead_assigned": (
            NotificationType(
                "Lead assigned to you", "Sales", "in_app", False, _pick("owner_id"), _render_lead_assigned
            ),
            LeadAssigned,
        ),
        "crm.opportunity_assigned": (
            NotificationType(
                "Opportunity assigned to you",
                "Sales",
                "in_app",
                False,
                _pick("owner_id"),
                _render_opp_assigned,
            ),
            OpportunityAssigned,
        ),
    }
    for key, (definition, event_cls) in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            continue
        register_event_source(event_cls, key)


_register_notifications()
