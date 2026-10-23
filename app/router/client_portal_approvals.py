"""Client Portal approvals hub and delegation (Epic 18, FR-102): ``/api/v1/client-portal/approvals*`` and
``/delegations*``. Timesheets are decided on Epic 25's ``/client-portal/timesheets`` endpoints (the generic decide
answers 409 ``use_subject_endpoint``). Decisions need an ``Idempotency-Key``."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.core.field_visibility import portal_guard_route
from app.core.idempotency import HEADER
from app.dependencies.client import ClientDBSessionDep, ClientDep
from app.models.client_portal import ClientUserDelegation
from app.models.external_auth import ClientUser
from app.schemas.client_hub_portal import (
    ApprovalDecideIn,
    ApprovalDetailOut,
    ApprovalsHubOut,
    ColleagueOut,
    DelegationIn,
    DelegationOut,
)
from app.services import client_delegations
from app.services.client_portal import approvals

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)


@router.get("/approvals", response_model=ApprovalsHubOut)
async def hub(_: ClientDep, db: ClientDBSessionDep) -> Any:
    return await approvals.hub(get_context(), db)


@router.get("/approvals/{request_id}", response_model=ApprovalDetailOut)
async def detail(request_id: uuid.UUID, _: ClientDep, db: ClientDBSessionDep) -> Any:
    return await approvals.detail(get_context(), db, request_id)


@router.post("/approvals/{request_id}/decide", response_model=ApprovalDetailOut)
async def decide(
    request: Request, request_id: uuid.UUID, data: ApprovalDecideIn, _: ClientDep, db: ClientDBSessionDep
) -> Any:
    if not (request.headers.get(HEADER) or "").strip():
        raise ValidationFailed("The Idempotency-Key header is required", code="idempotency_key_required")
    return await approvals.decide(
        get_context(), db, request_id, data.decision, data.comment, data.subject_payload
    )


# ---------------------------------------------------------------- delegation
def _state(row: ClientUserDelegation) -> str:
    today = datetime.now(UTC).date()
    if row.revoked_at is not None:
        return "revoked"
    if row.ends_on < today:
        return "past"
    return "future" if row.starts_on > today else "current"


async def _outs(db: Any, me: uuid.UUID, rows: list[ClientUserDelegation]) -> list[DelegationOut]:
    ids = {r.client_user_id for r in rows} | {r.delegate_client_user_id for r in rows}
    names = dict(
        (
            await db.execute(
                select(ClientUser.id, ClientUser.display_name).where(ClientUser.id.in_(ids or {me}))
            )
        ).all()
    )
    return [
        DelegationOut(
            id=r.id,
            client_user_id=r.client_user_id,
            client_user_name=names.get(r.client_user_id),
            delegate_client_user_id=r.delegate_client_user_id,
            delegate_name=names.get(r.delegate_client_user_id),
            starts_on=r.starts_on,
            ends_on=r.ends_on,
            revoked_at=r.revoked_at,
            state=_state(r),
            mine=r.client_user_id == me,
        )
        for r in rows
    ]


@router.get("/delegations", response_model=list[DelegationOut])
async def list_delegations(principal: ClientDep, db: ClientDBSessionDep) -> Any:
    assert principal.id is not None
    return await _outs(db, principal.id, await client_delegations.list_for(db, principal.id))


@router.get("/delegations/colleagues", response_model=list[ColleagueOut])
async def colleagues(principal: ClientDep, db: ClientDBSessionDep) -> Any:
    rows = (
        await db.execute(
            select(ClientUser)
            .where(
                ClientUser.client_id == principal.org_id,
                ClientUser.status == "active",
                ClientUser.id != principal.id,
            )
            .order_by(ClientUser.display_name)
        )
    ).scalars()
    return [
        ColleagueOut(id=r.id, display_name=r.display_name)
        for r in rows
        if client_delegations.DELEGATE_PERMISSION in (r.portal_permissions or [])
    ]


@router.post("/delegations", response_model=DelegationOut, status_code=status.HTTP_201_CREATED)
async def create_delegation(data: DelegationIn, principal: ClientDep, db: ClientDBSessionDep) -> Any:
    assert principal.id is not None
    row = await client_delegations.create(
        get_context(), db, principal.id, data.delegate_client_user_id, data.starts_on, data.ends_on
    )
    return (await _outs(db, principal.id, [row]))[0]


@router.post("/delegations/{delegation_id}/revoke", response_model=DelegationOut)
async def revoke_delegation(delegation_id: uuid.UUID, principal: ClientDep, db: ClientDBSessionDep) -> Any:
    assert principal.id is not None
    row = await client_delegations.revoke(db, principal.id, delegation_id)
    return (await _outs(db, principal.id, [row]))[0]
