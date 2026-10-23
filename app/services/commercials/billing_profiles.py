"""Billing Profile (Epic 16 A2, FR-88): one per client, addresses with GSTIN validation, billing contacts.

Billing / receivables read it through ``get_billing_profile`` (snapshotted at issue) and gate issue with
``assert_invoiceable``. Issued documents are never altered by profile edits.
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.commercials import BillingAddress, BillingProfile, BillingProfileContact, PaymentTerm
from app.models.reference import GstState
from app.schemas.commercials import (
    BillingAddressFacts,
    BillingAddressIn,
    BillingAddressOut,
    BillingAddressUpdate,
    BillingContactOut,
    BillingContactsFacts,
    BillingContactsIn,
    BillingProfileFacts,
    BillingProfileIn,
    BillingProfileOut,
)
from app.services.commercials import payment_terms
from app.services.commercials.common import conflict, invalid, now, require_client, tenant_of


def _incomplete() -> Exception:
    return conflict(
        "billing_profile_incomplete",
        "Invoices can't be issued until the client has a billing profile with an address",
    )


async def _profile(db: AsyncSession, client_id: uuid.UUID, *, lock: bool = False) -> BillingProfile | None:
    stmt = select(BillingProfile).where(BillingProfile.client_id == client_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _addresses(
    db: AsyncSession, profile_id: uuid.UUID, *, active_only: bool = True
) -> list[BillingAddress]:
    stmt = select(BillingAddress).where(BillingAddress.billing_profile_id == profile_id)
    if active_only:
        stmt = stmt.where(BillingAddress.archived_at.is_(None))
    return list(
        (
            await db.execute(stmt.order_by(BillingAddress.is_default.desc(), BillingAddress.created_at))
        ).scalars()
    )


async def _contacts(db: AsyncSession, profile_id: uuid.UUID, client_id: uuid.UUID) -> list[BillingContactOut]:
    from app.services import clients_service

    rows = list(
        (
            await db.execute(
                select(BillingProfileContact).where(BillingProfileContact.billing_profile_id == profile_id)
            )
        ).scalars()
    )
    facts = await clients_service.contacts_of_client(db, client_id, [r.contact_id for r in rows])
    out: list[BillingContactOut] = []
    for r in rows:
        f = facts.get(r.contact_id)
        if f is not None:
            out.append(
                BillingContactOut(contact_id=r.contact_id, role=r.role, name=f["name"], email=f["email"])
            )
    return out


async def _out(db: AsyncSession, profile: BillingProfile) -> BillingProfileOut:
    addresses = await _addresses(db, profile.id)
    out = BillingProfileOut.model_validate(profile)
    return out.model_copy(
        update={
            "addresses": [BillingAddressOut.model_validate(a) for a in addresses],
            "contacts": await _contacts(db, profile.id, profile.client_id),
            "complete": bool(addresses),
        }
    )


# ================================================================ validation
async def _check_address(db: AsyncSession, profile: BillingProfile, addr: dict[str, Any]) -> None:
    country = (addr.get("country") or "IN").upper()
    state_code = addr.get("state_code")
    gstin = addr.get("gstin") or None
    treatment = profile.tax_treatment
    if country == "IN":
        if state_code is None:
            raise invalid("state_required", "Select the GST state of an Indian address", field="state_code")
        if (await db.execute(select(GstState.code).where(GstState.code == state_code))).first() is None:
            raise invalid("unknown_state_code", "Unknown GST state code", state_code=state_code)
        if treatment == "export":
            raise invalid(
                "export_requires_foreign_address", "An export client needs a billing address outside India"
            )
        if treatment in ("registered", "sez"):
            if gstin is None:
                raise invalid(
                    "gstin_required",
                    "A GSTIN is required for a registered Indian billing address",
                    field="gstin",
                )
            from app.services import legal_entities_service

            errors = legal_entities_service.validate_gstin(gstin, state_code=state_code, pan=profile.pan)
            if errors:
                codes = [e.code for e in errors]
                reason = (
                    "state_mismatch" if "gstin_state_mismatch" in codes else codes[0].removeprefix("gstin_")
                )
                raise ValidationFailed(
                    errors[0].message,
                    code="gstin_invalid",
                    details={
                        "reason": reason,
                        "fields": [{"field": e.field, "code": e.code, "message": e.message} for e in errors],
                    },
                )
    elif gstin is not None:
        raise invalid("gstin_not_applicable", "A GSTIN applies to Indian addresses only", field="gstin")
    if treatment == "sez" and not addr.get("is_sez"):
        raise invalid(
            "sez_address_required", "A SEZ client's billing address must be marked SEZ", field="is_sez"
        )


def _addr_dict(a: BillingAddress) -> dict[str, Any]:
    return {"country": a.country, "state_code": a.state_code, "gstin": a.gstin, "is_sez": a.is_sez}


# ================================================================ profile
async def get_profile(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
) -> BillingProfileOut | None:
    await require_client(db, client_id)
    profile = await _profile(db, client_id)
    return None if profile is None else await _out(db, profile)


async def put_profile(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    data: BillingProfileIn,
) -> BillingProfileOut:
    await require_client(db, client_id)
    await payment_terms.assert_usable(db, data.payment_terms_id)
    profile = await _profile(db, client_id, lock=True)
    values = data.model_dump()
    if profile is None:
        profile = BillingProfile(id=uuid.uuid4(), tenant_id=tenant_of(db), client_id=client_id, **values)
        db.add(profile)
    else:
        for key, value in values.items():
            setattr(profile, key, value)
    for addr in await _addresses(db, profile.id) if profile.created_at is not None else []:
        await _check_address(db, profile, _addr_dict(addr))
    await db.flush()
    await db.refresh(profile)
    return await _out(db, profile)


async def _profile_or_404(db: AsyncSession, client_id: uuid.UUID) -> BillingProfile:
    profile = await _profile(db, client_id, lock=True)
    if profile is None:
        raise conflict(
            "billing_profile_missing", "Save the billing profile before adding addresses or contacts"
        )
    return profile


async def _clear_default(db: AsyncSession, profile_id: uuid.UUID, keep: uuid.UUID) -> None:
    await db.execute(
        update(BillingAddress)
        .where(
            BillingAddress.billing_profile_id == profile_id,
            BillingAddress.id != keep,
            BillingAddress.is_default.is_(True),
        )
        .values(is_default=False)
    )


async def add_address(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    data: BillingAddressIn,
) -> BillingAddressOut:
    await require_client(db, client_id)
    profile = await _profile_or_404(db, client_id)
    values = data.model_dump()
    await _check_address(db, profile, values)
    first = not await _addresses(db, profile.id)
    row = BillingAddress(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        client_id=client_id,
        billing_profile_id=profile.id,
        **{**values, "is_default": values["is_default"] or first},
    )
    if row.is_default and not first:
        await _clear_default(db, profile.id, row.id)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return BillingAddressOut.model_validate(row)


async def _address(db: AsyncSession, address_id: uuid.UUID) -> BillingAddress:
    row = (
        await db.execute(select(BillingAddress).where(BillingAddress.id == address_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Billing address not found")
    return row


async def update_address(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    address_id: uuid.UUID,
    data: BillingAddressUpdate,
) -> BillingAddressOut:
    row = await _address(db, address_id)
    if row.archived_at is not None:
        raise conflict("billing_address_archived", "This billing address is archived")
    profile = (
        await db.execute(select(BillingProfile).where(BillingProfile.id == row.billing_profile_id))
    ).scalar_one()
    changes = data.model_dump(exclude_unset=True)
    merged = {
        **_addr_dict(row),
        **{k: v for k, v in changes.items() if k in ("country", "state_code", "gstin", "is_sez")},
    }
    await _check_address(db, profile, merged)
    if changes.get("is_default") is False and row.is_default:
        raise conflict("billing_address_default_required", "Make another address the default instead")
    for key, value in changes.items():
        if key == "is_default" and value is None:
            continue
        setattr(row, key, value)
    if row.is_default:
        await _clear_default(db, profile.id, row.id)
    await db.flush()
    await db.refresh(row)
    return BillingAddressOut.model_validate(row)


async def archive_address(ctx: RequestContext, db: AsyncSession, address_id: uuid.UUID) -> BillingAddressOut:  # ruff: ignore[unused-function-argument]
    row = await _address(db, address_id)
    if row.archived_at is not None:
        return BillingAddressOut.model_validate(row)
    active = (
        await db.execute(
            select(func.count())
            .select_from(BillingAddress)
            .where(
                BillingAddress.billing_profile_id == row.billing_profile_id,
                BillingAddress.archived_at.is_(None),
            )
        )
    ).scalar_one()
    if active <= 1:
        raise conflict("billing_address_required", "A billing profile needs at least one billing address")
    was_default = row.is_default
    row.archived_at = now()
    row.is_default = False
    await db.flush()
    if was_default:
        nxt = (
            await db.execute(
                select(BillingAddress)
                .where(
                    BillingAddress.billing_profile_id == row.billing_profile_id,
                    BillingAddress.archived_at.is_(None),
                )
                .order_by(BillingAddress.created_at)
                .limit(1)
            )
        ).scalar_one()
        nxt.is_default = True
        await db.flush()
    await db.refresh(row)
    return BillingAddressOut.model_validate(row)


async def put_contacts(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    data: BillingContactsIn,
) -> BillingProfileOut:
    from app.services import clients_service

    await require_client(db, client_id)
    profile = await _profile_or_404(db, client_id)
    wanted: dict[uuid.UUID, str] = dict.fromkeys(data.cc, "cc")
    wanted.update(dict.fromkeys(data.to, "to"))
    facts = await clients_service.contacts_of_client(db, client_id, list(wanted))
    for contact_id in wanted:
        f = facts.get(contact_id)
        if f is None:
            raise invalid(
                "contact_invalid", "The contact does not belong to this client", contact_id=str(contact_id)
            )
        if not f.get("email"):
            raise invalid(
                "contact_email_missing", "Billing contacts need an email address", contact_id=str(contact_id)
            )
    await db.execute(
        delete(BillingProfileContact).where(BillingProfileContact.billing_profile_id == profile.id)
    )
    tenant_id = tenant_of(db)
    for contact_id, role in wanted.items():
        db.add(
            BillingProfileContact(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_id=client_id,
                billing_profile_id=profile.id,
                contact_id=contact_id,
                role=role,
            )
        )
    await db.flush()
    return await _out(db, profile)


# ================================================================ read API for billing / receivables
async def get_billing_profile(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
) -> BillingProfileFacts:
    profile = await _profile(db, client_id)
    if profile is None:
        raise _incomplete()
    addresses = await _addresses(db, profile.id)
    contacts = await _contacts(db, profile.id, client_id)
    days = None
    if profile.payment_terms_id is not None:
        days = (
            await db.execute(select(PaymentTerm.days).where(PaymentTerm.id == profile.payment_terms_id))
        ).scalar_one_or_none()
    if days is None:
        days = await payment_terms.payment_terms_days(db, client_id, None)
    return BillingProfileFacts(
        client_id=client_id,
        legal_name=profile.legal_name,
        pan=profile.pan,
        payment_terms_days=days,
        addresses=[
            BillingAddressFacts(
                id=a.id,
                state_code=a.state_code,
                gstin=a.gstin,
                sez=a.is_sez,
                country=a.country,
                is_default=a.is_default,
            )
            for a in addresses
        ],
        tax_treatment=profile.tax_treatment,
        po_required=profile.po_required,
        billing_contacts=BillingContactsFacts(
            to=[c.email for c in contacts if c.role == "to" and c.email],
            cc=[c.email for c in contacts if c.role == "cc" and c.email],
        ),
        tds_section_code=profile.tds_section_code,
        tds_rate_pct=profile.tds_rate_pct,
        gst_tds_applicable=profile.gst_tds_applicable,
        delivery_preference=profile.delivery_preference,
    )


async def assert_invoiceable(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    profile = await _profile(db, client_id)
    if profile is None or not await _addresses(db, profile.id):
        raise _incomplete()


async def erase_contact_links(db: AsyncSession, contact_id: uuid.UUID) -> int:
    """Erasure (DPDP): drop billing-contact link rows of an erased Contact (no PII is copied here)."""
    result = await db.execute(
        delete(BillingProfileContact).where(BillingProfileContact.contact_id == contact_id)
    )
    return int(result.rowcount or 0)
