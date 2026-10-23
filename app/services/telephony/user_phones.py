# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Recruiter calling phones (Epic 76, story 76.2, spec A8 / B2).

The phone is sealed with the vault field encryption (``vault_service.seal_field``, context
``user_call_phones.phone:<id>``); only ``last4`` is ever shown. Verification: a 6-digit OTP sent through the SMS port
(valid 10 min, 5 attempts; Fake in tests through ``set_otp_sender``), or an admin attestation when no SMS provider
exists (``integration:manage``, human only, audited).
"""

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.encrypted import blind_index
from app.models.telephony import UserCallPhone
from app.schemas.telephony import CallPhoneOut
from app.services import vault_service
from app.services.telephony import common

OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OtpSender = Callable[[RequestContext, AsyncSession, str, str], Awaitable[None]]


async def _sms_sender(ctx: RequestContext, db: AsyncSession, phone: str, code: str) -> None:
    from app.services.connectors.registry import get as adapter_get

    adapter = await adapter_get(ctx, db, "sms")
    send = getattr(adapter, "send", None)
    if send is None:
        raise common.conflict("sms_unavailable", "Talent OS can't send a code right now. Ask an admin to confirm your phone.")
    try:
        await send(phone, None, "talentos_call_phone_otp", None, [code], f"{code} is your Talent OS calling-phone code. It expires in 10 minutes.")
    except Exception as exc:
        logger.info("Calling-phone code not sent: {}", type(exc).__name__)
        raise common.conflict("sms_unavailable", "Talent OS can't send a code right now. Ask an admin to confirm your phone.") from None


_sender: OtpSender = _sms_sender


def set_otp_sender(fn: OtpSender | None) -> None:
    """Tests: capture the OTP instead of sending an SMS."""
    global _sender
    _sender = fn or _sms_sender


def _context(row_id: uuid.UUID) -> str:
    return f"user_call_phones.phone:{row_id}"


def _otp_hash(row_id: uuid.UUID, code: str) -> str:
    return hashlib.sha256(f"{row_id}:{code}".encode()).hexdigest()


def phone_hash(tenant_id: uuid.UUID, phone: str) -> str:
    return hashlib.sha256(blind_index(tenant_id, phone).encode()).hexdigest()


async def _live(db: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False) -> UserCallPhone | None:
    stmt = select(UserCallPhone).where(UserCallPhone.user_id == user_id, UserCallPhone.status != "revoked")
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def _out(row: UserCallPhone | None) -> CallPhoneOut:
    if row is None:
        return CallPhoneOut(status="none")
    return CallPhoneOut(
        status=row.status,
        last4=row.phone_last4,
        verified_at=row.verified_at,
        verification_method=row.verification_method,
        otp_expires_at=row.otp_expires_at if row.status == "pending" else None,
    )


def _me(ctx: RequestContext) -> uuid.UUID:
    uid = common.user_id_of(ctx)
    if uid is None:
        require_human(actor_from_ctx(ctx))
        raise common.conflict("human_only_action", "Only a person can manage a calling phone")
    return uid


async def _replace(db: AsyncSession, user_id: uuid.UUID, phone: str) -> UserCallPhone:
    existing = await _live(db, user_id, for_update=True)
    if existing is not None:
        existing.status = "revoked"
        await db.flush()
    tenant_id = common.tenant_of(db)
    row_id = uuid.uuid4()
    row = UserCallPhone(
        id=row_id,
        tenant_id=tenant_id,
        user_id=user_id,
        phone_cipher=await vault_service.seal_field(db, tenant_id, _context(row_id), phone),
        phone_last4=phone[-4:],
        phone_hash=phone_hash(tenant_id, phone),
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


async def mine(ctx: RequestContext, db: AsyncSession) -> CallPhoneOut:
    return _out(await _live(db, _me(ctx)))


async def start(ctx: RequestContext, db: AsyncSession, phone_e164: str) -> CallPhoneOut:
    """Register (or replace) the caller's phone and send a 6-digit code."""
    require_human(actor_from_ctx(ctx))
    uid = _me(ctx)
    phone = common.normalize_e164(phone_e164)
    if phone is None:
        raise common.invalid("invalid_phone", "Enter a mobile number in international format", fields={"phone_e164": "Invalid"})
    row = await _replace(db, uid, phone)
    code = f"{secrets.randbelow(1_000_000):06d}"
    row.otp_hash = _otp_hash(row.id, code)
    row.otp_expires_at = common.now() + OTP_TTL
    row.otp_attempts = 0
    await db.flush()
    await _sender(ctx, db, phone, code)
    audit.record("telephony.call_phone_added", "user_call_phone", row.id, session=db, last4=row.phone_last4)
    return _out(row)


async def verify(ctx: RequestContext, db: AsyncSession, code: str) -> CallPhoneOut:
    require_human(actor_from_ctx(ctx))
    row = await _live(db, _me(ctx), for_update=True)
    if row is None or row.status != "pending" or row.otp_hash is None:
        raise common.conflict("no_pending_code", "Send a code to your phone first")
    if row.otp_attempts >= OTP_MAX_ATTEMPTS:
        raise common.conflict("too_many_attempts", "Too many wrong codes. Send a new code.")
    if row.otp_expires_at is None or row.otp_expires_at < common.now():
        raise common.conflict("code_expired", "The code has expired. Send a new code.")
    if not hmac.compare_digest(row.otp_hash, _otp_hash(row.id, code)):
        row.otp_attempts += 1
        await db.flush()
        raise common.invalid("wrong_code", "That code isn't right", attempts_left=OTP_MAX_ATTEMPTS - row.otp_attempts)
    row.status = "verified"
    row.verified_at = common.now()
    row.verification_method = "otp_sms"
    row.otp_hash = None
    row.otp_expires_at = None
    await db.flush()
    audit.record("telephony.call_phone_verified", "user_call_phone", row.id, session=db, method="otp_sms")
    return _out(row)


async def remove(ctx: RequestContext, db: AsyncSession) -> None:
    row = await _live(db, _me(ctx), for_update=True)
    if row is not None:
        row.status = "revoked"
        await db.flush()
        audit.record("telephony.call_phone_removed", "user_call_phone", row.id, session=db)


async def attest(ctx: RequestContext, db: AsyncSession, user_id: uuid.UUID, phone_e164: str, reason: str) -> CallPhoneOut:
    """Admin attestation (no SMS provider): human only, audited with the reason."""
    require_human(actor_from_ctx(ctx))
    admin = _me(ctx)
    from app.models.identity import User

    if (await db.execute(select(User.id).where(User.id == user_id))).scalar_one_or_none() is None:
        raise common.not_found("User")
    phone = common.normalize_e164(phone_e164)
    if phone is None:
        raise common.invalid("invalid_phone", "Enter a mobile number in international format", fields={"phone_e164": "Invalid"})
    row = await _replace(db, user_id, phone)
    row.status = "verified"
    row.verified_at = common.now()
    row.verification_method = "admin_attested"
    row.attested_by = admin
    await db.flush()
    audit.record("telephony.call_phone_attested", "user_call_phone", row.id, session=db, reason=reason[:300], user_id=str(user_id))
    return _out(row)


async def verified_phone(db: AsyncSession, user_id: uuid.UUID) -> tuple[UserCallPhone, str] | None:
    """The caller's verified phone, opened from the vault (call path only; never returned or logged)."""
    row = await _live(db, user_id)
    if row is None or row.status != "verified":
        return None
    return row, await vault_service.open_field(db, row.tenant_id, _context(row.id), row.phone_cipher)


async def verified_row(db: AsyncSession, user_id: uuid.UUID) -> UserCallPhone | None:
    row = await _live(db, user_id)
    return row if row is not None and row.status == "verified" else None
