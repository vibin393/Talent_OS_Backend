"""SMS / WhatsApp opt-outs (Epic 33, FR-172/173): ``channel_opt_outs``, one active row per (channel, phone).

Suppression is keyed on ``phone_hash`` (keyed SHA-256), so it survives erasure of the phone number itself.
Checked at enqueue **and** again in the send task. Never logs phone numbers.
"""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.events.comms import ChannelOptOutRecorded
from app.models.comms import ChannelOptOut
from app.schemas.messaging import OptOutIn, OptOutOut
from app.services.connectors.sms.channel import mask_phone

STOP_WORDS = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "END", "CANCEL", "QUIT", "OPTOUT", "OPT-OUT", "OPT OUT"}
)


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def phone_hash(phone: str) -> str:
    key = hashlib.sha256(
        f"talent-os-phone-hash:{get_settings().token_encryption_key or 'dev'}".encode()
    ).digest()
    return hmac.new(key, phone.strip().encode(), hashlib.sha256).hexdigest()


def is_stop(text: str | None) -> bool:
    return (text or "").strip().upper().strip(".!") in STOP_WORDS


async def active_for(db: AsyncSession, channel: str, phone: str | None) -> ChannelOptOut | None:
    if not phone:
        return None
    return (
        await db.execute(
            select(ChannelOptOut).where(
                ChannelOptOut.channel == channel,
                ChannelOptOut.phone_hash == phone_hash(phone),
                ChannelOptOut.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def is_opted_out(db: AsyncSession, channel: str, phone: str | None) -> bool:
    return await active_for(db, channel, phone) is not None


async def record(
    db: AsyncSession,
    channel: str,
    phone: str,
    source: str,
    *,
    recipient_kind: str | None = None,
    recipient_id: uuid.UUID | None = None,
    reason: str | None = None,
    created_by: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Insert an active opt-out (idempotent: an existing active one wins). Returns the new row id or None."""
    tenant_id = _tenant(db)
    new_id = (
        await db.execute(
            pg_insert(ChannelOptOut)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                channel=channel,
                phone_e164=phone,
                phone_hash=phone_hash(phone),
                source=source,
                recipient_kind=recipient_kind,
                recipient_id=recipient_id,
                candidate_id=recipient_id if recipient_kind == "candidate" else None,
                reason=reason,
                created_by=created_by,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "channel", "phone_hash"],
                index_where=ChannelOptOut.revoked_at.is_(None),
            )
            .returning(ChannelOptOut.id)
        )
    ).scalar_one_or_none()
    if new_id is not None:
        emit(
            db,
            ChannelOptOutRecorded(
                tenant_id=tenant_id,
                opt_out_id=new_id,
                channel=channel,
                source=source,
                candidate_id=recipient_id if recipient_kind == "candidate" else None,
            ),
        )
    return new_id


async def revoke_for(db: AsyncSession, channel: str, phone: str, reason: str) -> int:
    rows = list(
        (
            await db.execute(
                select(ChannelOptOut).where(
                    ChannelOptOut.channel == channel,
                    ChannelOptOut.phone_hash == phone_hash(phone),
                    ChannelOptOut.revoked_at.is_(None),
                )
            )
        ).scalars()
    )
    for row in rows:
        row.revoked_at, row.revoke_reason = _now(), reason[:500]
    await db.flush()
    return len(rows)


# ================================================================ admin API
def _out(row: ChannelOptOut) -> OptOutOut:
    return OptOutOut(
        id=row.id,
        channel=row.channel,
        phone_masked=mask_phone(row.phone_e164),
        source=row.source,
        recipient_kind=row.recipient_kind,
        recipient_id=row.recipient_id,
        reason=row.reason,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


async def list_opt_outs(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    channel: str | None = None,
    q: str | None = None,
    include_revoked: bool = False,
) -> list[OptOutOut]:
    stmt = select(ChannelOptOut)
    if channel:
        stmt = stmt.where(ChannelOptOut.channel == channel)
    if not include_revoked:
        stmt = stmt.where(ChannelOptOut.revoked_at.is_(None))
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        if digits:
            stmt = stmt.where(
                or_(
                    ChannelOptOut.phone_e164.contains(digits),
                    ChannelOptOut.phone_hash == phone_hash(f"+{digits}"),
                )
            )
    rows = (await db.execute(stmt.order_by(ChannelOptOut.created_at.desc()).limit(500))).scalars()
    return [_out(r) for r in rows]


async def add_opt_out(ctx: RequestContext, db: AsyncSession, data: OptOutIn) -> OptOutOut:
    principal = ctx.principal
    created_by = principal.id if isinstance(principal, Principal) and principal.type == "user" else None
    new_id = await record(
        db, data.channel, data.phone_e164, "user", reason=data.reason, created_by=created_by
    )
    if new_id is None:
        raise ConflictError("This number is already opted out", code="already_opted_out")
    row = await db.get(ChannelOptOut, new_id)
    assert row is not None
    await db.refresh(row)
    return _out(row)


async def revoke_opt_out(
    ctx: RequestContext, db: AsyncSession, opt_out_id: uuid.UUID, reason: str
) -> OptOutOut:
    row = await db.get(ChannelOptOut, opt_out_id)
    if row is None:
        raise NotFound("Opt-out not found")
    if row.revoked_at is not None:
        raise ConflictError("This opt-out is already revoked", code="opt_out_revoked")
    row.revoked_at, row.revoke_reason = _now(), reason[:500]
    await db.flush()
    await db.refresh(row)
    return _out(row)
