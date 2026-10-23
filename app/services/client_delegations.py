"""Client User out-of-office delegation (Epic 18, FR-102). Low-level module (no upward imports) so domain epics
(timesheets, expenses, ...) and the approvals engine can call ``acting_for``.

* A delegate is a Client User of the same client, active, holding ``time:approve`` and not restricted more
  narrowly than the delegator (422 ``delegate_scope_narrower``). Overlapping live ranges of one delegator are
  refused (409 ``delegation_overlap``).
* ``acting_for(db, client_user_id, on_date)`` is the single check: the delegators the user may act for that day.
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.client_portal import ClientDelegationCreated
from app.models.client_portal import ClientUserDelegation
from app.models.external_auth import ClientUser, ClientUserScope

DELEGATE_PERMISSION = "time:approve"


def _today() -> date:
    return datetime.now(UTC).date()


async def acting_for(
    db: AsyncSession, client_user_id: uuid.UUID, on_date: date | None = None
) -> set[uuid.UUID]:
    day = on_date or _today()
    rows = await db.execute(
        select(ClientUserDelegation.client_user_id, ClientUser.status)
        .join(ClientUser, ClientUser.id == ClientUserDelegation.client_user_id)
        .where(
            ClientUserDelegation.delegate_client_user_id == client_user_id,
            ClientUserDelegation.revoked_at.is_(None),
            ClientUserDelegation.starts_on <= day,
            ClientUserDelegation.ends_on >= day,
        )
    )
    return {r[0] for r in rows.all() if r[1] == "active"}


async def delegate_resolver(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal_id: uuid.UUID,
    on_date: date,
) -> set[uuid.UUID]:
    """``approvals.register_delegate_resolver('client_user', ...)`` adapter."""
    return await acting_for(db, principal_id, on_date)


async def _scopes(db: AsyncSession, user_id: uuid.UUID) -> set[tuple[str, uuid.UUID]]:
    rows = await db.execute(
        select(ClientUserScope.subject_kind, ClientUserScope.subject_id).where(
            ClientUserScope.client_user_id == user_id
        )
    )
    return {(r[0], r[1]) for r in rows.all()}


async def _user(db: AsyncSession, user_id: uuid.UUID) -> ClientUser | None:
    return (await db.execute(select(ClientUser).where(ClientUser.id == user_id))).scalar_one_or_none()


async def create(
    ctx: RequestContext,
    db: AsyncSession,
    delegator_id: uuid.UUID,
    delegate_id: uuid.UUID,
    starts_on: date,
    ends_on: date,
) -> ClientUserDelegation:
    if ends_on < starts_on:
        raise ValidationFailed("The end date must be on or after the start date", code="invalid_range")
    if ends_on < _today():
        raise ValidationFailed("The delegation must not end in the past", code="invalid_range")
    if delegate_id == delegator_id:
        raise ValidationFailed("You cannot delegate to yourself", code="delegate_self")
    me = await _user(db, delegator_id)
    other = await _user(db, delegate_id)
    if me is None:
        raise NotFound("Client user not found")
    if other is None or other.client_id != me.client_id or other.status != "active":
        raise NotFound("Colleague not found")
    if DELEGATE_PERMISSION not in (other.portal_permissions or []):
        raise ValidationFailed(
            "The colleague cannot approve on your behalf", code="delegate_missing_permission"
        )
    if other.restricted:
        mine = await _scopes(db, me.id)
        theirs = await _scopes(db, other.id)
        if not me.restricted or not mine <= theirs:
            raise ValidationFailed(
                "The colleague sees fewer workers than you", code="delegate_scope_narrower"
            )
    existing = (
        await db.execute(
            select(ClientUserDelegation.id).where(
                ClientUserDelegation.client_user_id == delegator_id,
                ClientUserDelegation.revoked_at.is_(None),
                ClientUserDelegation.starts_on <= ends_on,
                ClientUserDelegation.ends_on >= starts_on,
            )
        )
    ).first()
    if existing is not None:
        raise ConflictError("You already have a delegation in that period", code="delegation_overlap")
    row = ClientUserDelegation(
        id=uuid.uuid4(),
        tenant_id=me.tenant_id,
        client_id=me.client_id,
        client_user_id=delegator_id,
        delegate_client_user_id=delegate_id,
        starts_on=starts_on,
        ends_on=ends_on,
    )
    db.add(row)
    await db.flush()
    emit(
        db,
        ClientDelegationCreated(
            tenant_id=row.tenant_id,
            delegation_id=row.id,
            client_user_id=delegator_id,
            delegate_client_user_id=delegate_id,
        ),
    )
    return row


async def revoke(
    db: AsyncSession, client_user_id: uuid.UUID, delegation_id: uuid.UUID
) -> ClientUserDelegation:
    row = (
        await db.execute(
            select(ClientUserDelegation).where(
                ClientUserDelegation.id == delegation_id,
                ClientUserDelegation.client_user_id == client_user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise NotFound("Delegation not found")
    row.revoked_at = datetime.now(UTC)
    await db.flush()
    return row


async def list_for(db: AsyncSession, client_user_id: uuid.UUID) -> list[ClientUserDelegation]:
    rows = await db.execute(
        select(ClientUserDelegation)
        .where(
            (ClientUserDelegation.client_user_id == client_user_id)
            | (ClientUserDelegation.delegate_client_user_id == client_user_id)
        )
        .order_by(ClientUserDelegation.starts_on.desc())
        .limit(200)
    )
    return list(rows.scalars())
