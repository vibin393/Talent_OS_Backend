# ruff: noqa: E501
"""Caller IDs (Epic 76, story 76.1): the Tenant's registered virtual numbers per legal entity (NULL = tenant default),
one active per entity. Numbers are bought in the provider console and registered here with the regulatory reference
(ExoPhone KYC id, Twilio bundle SID, Plivo compliance id) and the admin-entered per-minute rate."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.oauth import AppInstallation
from app.models.telephony import PROVIDER_KEYS, TelephonyNumber
from app.schemas.telephony import TelephonyNumberIn, TelephonyNumberOut, TelephonyNumberUpdate
from app.services.telephony import common


async def _get(db: AsyncSession, number_id: uuid.UUID) -> TelephonyNumber:
    row = (await db.execute(select(TelephonyNumber).where(TelephonyNumber.id == number_id))).scalar_one_or_none()
    if row is None:
        raise common.not_found("Caller ID")
    return row


async def list_numbers(ctx: RequestContext, db: AsyncSession) -> list[TelephonyNumberOut]:  # noqa: ARG001
    rows = (await db.execute(select(TelephonyNumber).order_by(TelephonyNumber.created_at))).scalars()
    return [TelephonyNumberOut.model_validate(r) for r in rows]


async def create(ctx: RequestContext, db: AsyncSession, data: TelephonyNumberIn) -> TelephonyNumberOut:  # noqa: ARG001
    inst = (
        await db.execute(select(AppInstallation).where(AppInstallation.id == data.installation_id))
    ).scalar_one_or_none()
    if inst is None or inst.kind != "connector" or inst.connector_key not in PROVIDER_KEYS:
        raise common.invalid(
            "telephony_installation_invalid", "Choose an installed calling provider", fields={"installation_id": "Invalid"}
        )
    number = common.normalize_e164(data.number_e164)
    if number is None or len(number) > 16:
        raise common.invalid("invalid_phone", "Enter the number in international format", fields={"number_e164": "Invalid"})
    row = TelephonyNumber(
        id=uuid.uuid4(),
        tenant_id=common.tenant_of(db),
        installation_id=inst.id,
        legal_entity_id=data.legal_entity_id,
        provider_key=str(inst.connector_key),
        number_e164=number,
        label=data.label,
        regulatory_ref=data.regulatory_ref,
        rate_per_minute_minor=data.rate_per_minute_minor,
        status="active",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise common.conflict(
            "caller_id_exists", "An active caller ID already exists for this legal entity"
        ) from exc
    await db.refresh(row)
    return TelephonyNumberOut.model_validate(row)


async def update(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    number_id: uuid.UUID,
    data: TelephonyNumberUpdate,
) -> TelephonyNumberOut:
    row = await _get(db, number_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None or key in ("label", "regulatory_ref"):
            setattr(row, key, value)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise common.conflict(
            "caller_id_exists", "An active caller ID already exists for this legal entity"
        ) from exc
    await db.refresh(row)
    return TelephonyNumberOut.model_validate(row)


async def deactivate(ctx: RequestContext, db: AsyncSession, number_id: uuid.UUID) -> None:  # noqa: ARG001
    row = await _get(db, number_id)
    row.status = "inactive"
    await db.flush()


async def active_for(db: AsyncSession, legal_entity_id: uuid.UUID | None) -> TelephonyNumber | None:
    rows = list(
        (await db.execute(select(TelephonyNumber).where(TelephonyNumber.status == "active"))).scalars()
    )
    if legal_entity_id is not None:
        found = next((r for r in rows if r.legal_entity_id == legal_entity_id), None)
        if found is not None:
            return found
    return next((r for r in rows if r.legal_entity_id is None), None)


def caller_id_label(row: TelephonyNumber) -> str:
    return f"{row.label} ({row.number_e164})" if row.label else row.number_e164
