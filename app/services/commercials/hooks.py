"""Registrations of the ``commercials`` domain (autodiscovered by ``core.hooks``; Epic 16 A8).

* Approval subjects ``contract``, ``rate_exception``, ``po_exception`` (human-only deciders, AD-24 / FR-181).
* Posting source ``milestone`` (accepted, non-cancelled milestones of Active SOWs; billing ledger only).
* Workflow triggers ``commercials.contract_status_changed``, ``commercials.rate_card_published``,
  ``commercials.po_threshold_reached``, ``commercials.po_validity_changed``.
* Notification types ``commercials.contract_renewal_due``, ``commercials.po_threshold_reached``,
  ``commercials.po_expiring`` (PO owner + users holding ``billing:write``).
* Activity entities ``contract``, ``purchase_order``, ``rate_card`` (notes / tasks / timeline).
* ``SignatureRequestCompleted(subject_kind="contract")`` -> store the signed file, attempt activation.

Contact erasure: billing-contact links reference ``contacts`` with ``ON DELETE CASCADE`` and copy no PII; the
P1 DSR pipeline is candidate-only, so no erasure handler is registered (``billing_profiles.erase_contact_links``
is available for a future contact DSR).
"""  # ruff: ignore[line-too-long]

import uuid
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe
from app.services import activity_service, files_service
from app.services.approvals import register_approval_subject

# ---------------------------------------------------------------- approval subjects
from app.services.commercials import consumption, contracts, rate_checks
from app.services.notifications_service import (
    Content,
    NotificationType,
    register_event_source,
    register_notification_type,
)
from app.services.workflow.registry import Trigger, register_trigger

register_approval_subject(
    contracts.APPROVAL_SUBJECT, on_final_decided=contracts.on_final_decided, describe=contracts.describe
)
register_approval_subject(
    rate_checks.APPROVAL_SUBJECT, on_final_decided=rate_checks.on_final_decided, describe=rate_checks.describe
)
register_approval_subject(consumption.APPROVAL_SUBJECT, on_final_decided=consumption.on_po_exception_decided)


# ---------------------------------------------------------------- rate gates at the P1 proceed points
def _register_rate_gates() -> None:
    from app.services import rate_gate_port
    from app.services.commercials import gates

    rate_gate_port.register_rate_gate("demand_submit", gates.demand_submit)
    rate_gate_port.register_rate_gate("submission_create", gates.submission_create)
    rate_gate_port.register_rate_gate("offer_release", gates.offer_release)


_register_rate_gates()


# ---------------------------------------------------------------- posting source (AD-23)
async def _milestone_loader(ctx: RequestContext, db: AsyncSession, query: Any) -> list[Any]:  # ruff: ignore[unused-function-argument]
    from app.models.commercials import Contract, ContractMilestone
    from app.services.postings import SourceItem

    if query.ledger != "billing":
        return []
    stmt = (
        select(ContractMilestone, Contract.legal_entity_id)
        .join(Contract, Contract.id == ContractMilestone.contract_id)
        .where(
            ContractMilestone.status == "accepted",
            Contract.contract_type == "sow",
            Contract.status == "active",
        )
    )
    if query.legal_entity_id is not None:
        stmt = stmt.where(Contract.legal_entity_id == query.legal_entity_id)
    rows = (await db.execute(stmt)).all()
    items: list[Any] = []
    for milestone, legal_entity_id in rows:
        accepted_on = milestone.accepted_at.date() if milestone.accepted_at else query.period_end
        if accepted_on > query.period_end:
            continue
        items.append(
            SourceItem(
                source_kind="milestone",
                source_id=milestone.id,
                placement_id=None,
                item_date=accepted_on,
                quantity=Decimal("1"),
                amount=milestone.amount,
                currency=milestone.currency,
                attributes={
                    "category": "milestone",
                    "billable": True,
                    "legal_entity_id": str(legal_entity_id),
                    "approved_at": milestone.accepted_at.isoformat() if milestone.accepted_at else None,
                    "unit": "item",
                },
            )
        )
    return items


def _register_posting_source() -> None:
    from app.services.postings import register_posting_source

    try:
        register_posting_source("milestone", _milestone_loader, ledgers=frozenset({"billing"}))
    except Exception:
        logger.debug("milestone posting source already registered")


_register_posting_source()


# ---------------------------------------------------------------- workflow triggers
_MODULE = "app.events.commercials"
for _trigger in (
    Trigger(
        "commercials.contract_status_changed",
        "Contract status changed",
        _MODULE,
        "ContractStatusChanged",
        "contract",
        "contract_id",
    ),
    Trigger(
        "commercials.rate_card_published",
        "Rate card published",
        _MODULE,
        "RateCardPublished",
        "rate_card",
        "rate_card_id",
    ),
    Trigger(
        "commercials.po_threshold_reached",
        "PO consumption threshold reached",
        _MODULE,
        "PoConsumptionThresholdReached",
        "purchase_order",
        "po_id",
    ),
    Trigger(
        "commercials.po_validity_changed",
        "PO validity changed",
        _MODULE,
        "PoValidityChanged",
        "purchase_order",
        "po_id",
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


async def _billing_writers(db: AsyncSession) -> set[uuid.UUID]:
    from app.services import identity_bridge

    return {u["id"] for u in await identity_bridge.users_with_permission(db, "billing:write")}


async def _po_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.commercials import PurchaseOrder

    out = await _billing_writers(db)
    po_id = _uuid(payload.get("po_id"))
    if po_id is not None:
        owner = (
            await db.execute(select(PurchaseOrder.owner_id).where(PurchaseOrder.id == po_id))
        ).scalar_one_or_none()
        if owner is not None:
            out.add(owner)
    return out


async def _contract_owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    owner = _uuid(payload.get("owner_id"))
    return {owner} if owner else set()


async def _po_label(db: AsyncSession, payload: Any) -> str:
    from app.models.commercials import PurchaseOrder

    po_id = _uuid(payload.get("po_id"))
    number = None
    if po_id is not None:
        number = (
            await db.execute(select(PurchaseOrder.po_number).where(PurchaseOrder.id == po_id))
        ).scalar_one_or_none()
    return f"PO {number}" if number else "A purchase order"


async def _render_threshold(db: AsyncSession, payload: Any) -> Content:
    label = await _po_label(db, payload)
    po_id = _uuid(payload.get("po_id"))
    return Content(
        title=f"{label} reached {payload.get('pct')}% consumption"[:300],
        entity_type="purchase_order",
        entity_id=po_id,
        link_path=f"/purchase-orders/{po_id}" if po_id else None,
    )


async def _render_expiring(db: AsyncSession, payload: Any) -> Content:
    label = await _po_label(db, payload)
    po_id = _uuid(payload.get("po_id"))
    return Content(
        title=f"{label} expires in {payload.get('days_left')} days ({payload.get('valid_to')})"[:300],
        entity_type="purchase_order",
        entity_id=po_id,
        link_path=f"/purchase-orders/{po_id}" if po_id else None,
    )


async def _render_renewal(db: AsyncSession, payload: Any) -> Content:
    from app.models.commercials import Contract

    contract_id = _uuid(payload.get("contract_id"))
    human = None
    if contract_id is not None:
        human = (
            await db.execute(select(Contract.human_id).where(Contract.id == contract_id))
        ).scalar_one_or_none()
    return Content(
        title=f"Contract {human or ''} ends on {payload.get('end_date')} -- plan the renewal"[:300],
        entity_type="contract",
        entity_id=contract_id,
        link_path=f"/contracts/{contract_id}" if contract_id else None,
    )


def _register_notifications() -> None:
    from app.events.commercials import ContractRenewalDue, PoConsumptionThresholdReached, PoExpiring

    types = {
        "commercials.contract_renewal_due": (
            NotificationType(
                "Contract renewal due", "Commercials", "both", False, _contract_owner, _render_renewal
            ),
            ContractRenewalDue,
        ),
        "commercials.po_threshold_reached": (
            NotificationType(
                "PO consumption threshold reached",
                "Commercials",
                "both",
                False,
                _po_recipients,
                _render_threshold,
            ),
            PoConsumptionThresholdReached,
        ),
        "commercials.po_expiring": (
            NotificationType("PO expiring", "Commercials", "both", False, _po_recipients, _render_expiring),
            PoExpiring,
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


# ---------------------------------------------------------------- activity entities
async def _contract_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    return await _has(ctx, db, "contract", entity_id, write)


async def _po_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    return await _has(ctx, db, "po", entity_id, write)


async def _rate_card_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    return await _has(ctx, db, "rate_card", entity_id, write)


async def _has(
    ctx: RequestContext, db: AsyncSession, resource: str, entity_id: uuid.UUID, write: bool
) -> bool:
    from app.core.permissions import Principal
    from app.models.commercials import Contract, PurchaseOrder, RateCard

    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        return False
    if not principal.has(f"{resource}:{'write' if write else 'read'}"):
        return False
    model = {"contract": Contract, "po": PurchaseOrder, "rate_card": RateCard}[resource]
    return (await db.execute(select(model.id).where(model.id == entity_id))).first() is not None  # type: ignore[attr-defined]


activity_service.register_entity("contract", _contract_access, plural="contracts")
activity_service.register_entity("purchase_order", _po_access, plural="purchase-orders")
activity_service.register_entity("rate_card", _rate_card_access, plural="rate-cards")

# Files owned by contracts (documents) and POs (the PO document) are readable with the record's permission.
files_service.register_access_checker("contract", _contract_access, plural="contracts")
files_service.register_access_checker("purchase_order", _po_access, plural="purchase-orders")


# ---------------------------------------------------------------- e-signature completion (Epic 22 event)
def _on_signature_completed(event: Any) -> None:
    if getattr(event, "subject_kind", None) != "contract":
        return
    from app.tasks.commercials import signature_completed
    from app.tasks.runtime import enqueue

    enqueue(
        signature_completed,
        str(event.tenant_id),
        str(event.subject_id),
        str(event.request_id),
        str(event.document_id),
    )


def _subscribe_signatures() -> None:
    from app.events.documents import SignatureRequestCompleted

    subscribe(SignatureRequestCompleted, _on_signature_completed)


_subscribe_signatures()
