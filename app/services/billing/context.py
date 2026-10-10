# ruff: noqa: E501
"""Resolve the facts a document needs from the foundation / commercials (no tax logic here; ``tax.py`` decides).

``supply_for`` -> supplier registration, recipient (Billing Profile default address), treatment, LUT of the
document's financial year and the reverse-charge flag (Contract override, else ``billing_client_settings``), then
``tax.determine_supply``. ``fx_for`` -> the document currency's INR rate for the date (``legal_entities.fx``).
"""

import dataclasses
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import fiscal
from app.core.context import RequestContext
from app.core.errors import DomainError
from app.core.money import assert_document_currency
from app.models.billing import BillingClientSettings
from app.services import commercials_service, legal_entities_service
from app.services.billing import tax
from app.services.billing.common import invalid


@dataclasses.dataclass(frozen=True)
class Supply:
    decision: tax.SupplyDecision
    recipient: dict[str, Any]
    supplier: dict[str, Any]
    billing_address_id: uuid.UUID | None
    einvoice_registration: bool
    irp_limit: bool


@dataclasses.dataclass(frozen=True)
class Fx:
    rate: Decimal
    source: str
    rate_date: date


async def client_reverse_charge(db: AsyncSession, client_id: uuid.UUID) -> bool:
    row = (
        await db.execute(select(BillingClientSettings).where(BillingClientSettings.client_id == client_id))
    ).scalar_one_or_none()
    return bool(row and row.reverse_charge)


async def _client_name(db: AsyncSession, client_id: uuid.UUID) -> str | None:
    from app.models.clients import Client

    return (await db.execute(select(Client.name).where(Client.id == client_id))).scalar_one_or_none()


async def supply_for(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID,
    gst_registration_id: uuid.UUID | None,
    client_id: uuid.UUID,
    contract_id: uuid.UUID | None,
    doc_date: date,
) -> Supply:
    entity = await legal_entities_service.get_entity(ctx, db, legal_entity_id)
    supplier: tax.SupplierReg | None = None
    supplier_snap: dict[str, Any] = {
        "name": entity.name,
        "legal_name": entity.legal_name,
        "pan": entity.pan,
        "cin": entity.cin,
        "address": entity.address,
        "state_code": entity.state_code,
        "country": entity.country,
        "signatory_name": entity.signatory_name,
        "signatory_title": entity.signatory_title,
    }
    einvoice_reg = irp_limit = False
    lut: tax.Lut | None = None
    if entity.country == "IN":
        if gst_registration_id is None:
            raise invalid("gstin_missing", "The supplier GST registration is missing")
        reg = await legal_entities_service.get_gst_registration(ctx, db, gst_registration_id)
        supplier = tax.SupplierReg(reg.gstin, reg.state_code)
        supplier_snap.update(gstin=reg.gstin, state_code=reg.state_code, registration_address=reg.address)
        einvoice_reg, irp_limit = bool(reg.einvoice_applicable), bool(reg.irp_reporting_limit)
        lut_row = await legal_entities_service.get_lut(ctx, db, reg.id, fiscal.fiscal_year(doc_date))
        if lut_row is not None and lut_row.valid_from <= doc_date <= lut_row.valid_to:
            lut = tax.Lut(lut_row.lut_number, lut_row.fiscal_year)
    try:
        profile = await commercials_service.get_billing_profile(ctx, db, client_id)
    except DomainError as exc:
        raise invalid("billing_profile_missing", "The client has no complete billing profile") from exc
    if not profile.addresses:
        raise invalid("billing_profile_missing", "The client's billing profile has no address")
    address = next((a for a in profile.addresses if a.is_default), profile.addresses[0])
    treatment = profile.tax_treatment
    if address.sez:
        treatment = "sez"
    if (address.country or "IN").upper() != "IN":
        treatment = "export"
    if treatment == "registered" and not address.gstin:
        raise invalid("gstin_missing", "The client's billing address has no GSTIN")
    reverse = False
    if contract_id is not None:
        try:
            reverse = bool((await commercials_service.get_contract(ctx, db, contract_id)).reverse_charge)
        except DomainError:
            reverse = False
    reverse = reverse or await client_reverse_charge(db, client_id)
    recipient = tax.Recipient(address.gstin, address.state_code, address.country or "IN")
    try:
        decision = tax.determine_supply(supplier, recipient, treatment, lut, reverse)  # type: ignore[arg-type]
    except tax.TaxDecisionError as exc:
        raise invalid(exc.code, str(exc)) from exc
    snapshot = {
        "name": profile.legal_name,
        "display_name": await _client_name(db, client_id),
        "gstin": address.gstin,
        "pan": profile.pan,
        "state_code": address.state_code,
        "country": address.country,
        "treatment": treatment,
        "billing_address_id": str(address.id),
        "contacts": list(profile.billing_contacts.to),
    }
    return Supply(decision, snapshot, supplier_snap, address.id, einvoice_reg, irp_limit)


async def fx_for(ctx: RequestContext, db: AsyncSession, currency: str, on_date: date) -> Fx:
    assert_document_currency(currency)
    rate = await legal_entities_service.rate_for(ctx, db, currency, "INR", on_date)
    return Fx(Decimal(rate.rate), rate.source, rate.rate_date)
