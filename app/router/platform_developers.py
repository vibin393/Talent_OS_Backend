# ruff: file-ignore[line-too-long]
"""Platform console for the developer portal (Epic 74, spec A4; ``/api/v1/platform/developers``). Platform operator +
MFA. Verification decisions need the platform role ``marketplace_reviewer`` (403 ``platform_role_required``);
approve / reject are ★ (human platform operators only; ``app`` / ``api_key`` principals never reach this router)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import or_, select

from app.core.context import get_context
from app.core.database import platform_session
from app.core.errors import NotFound
from app.dependencies.platform import PlatformOperatorMfaDep
from app.models.developer import DeveloperAccount
from app.models.oauth import PartnerOrg
from app.schemas.developer import AccountDirectoryRow, DecisionIn, OrgOut, VerificationOut
from app.services import developer_portal_service as dev
from app.services.developer_portal import audit_log

router = APIRouter(prefix="/platform/developers", tags=["platform-developers"])


@router.get("/accounts", response_model=list[AccountDirectoryRow])
async def accounts(_: PlatformOperatorMfaDep, q: Annotated[str | None, Query(max_length=200)] = None) -> list[AccountDirectoryRow]:
    async with platform_session() as db:
        stmt = select(DeveloperAccount)
        if q:
            stmt = stmt.where(or_(DeveloperAccount.email.ilike(f"%{q}%"), DeveloperAccount.name.ilike(f"%{q}%")))
        rows = (await db.execute(stmt.order_by(DeveloperAccount.created_at.desc()).limit(200))).scalars()
        return [
            AccountDirectoryRow(id=a.id, email=str(a.email), name=a.name, status=a.status, email_verified=a.email_verified_at is not None,
                                mfa_enabled=a.mfa_enabled_at is not None, created_at=a.created_at, last_login_at=a.last_login_at)
            for a in rows
        ]  # fmt: skip


@router.get("/orgs", response_model=list[OrgOut])
async def orgs(
    _: PlatformOperatorMfaDep, q: Annotated[str | None, Query(max_length=200)] = None,
    verification_status: Annotated[str | None, Query(max_length=20)] = None,
) -> list[OrgOut]:
    async with platform_session() as db:
        stmt = select(PartnerOrg)
        if q:
            stmt = stmt.where(or_(PartnerOrg.name.ilike(f"%{q}%"), PartnerOrg.slug.ilike(f"%{q}%")))
        if verification_status:
            stmt = stmt.where(PartnerOrg.verification_status == verification_status)
        rows = (await db.execute(stmt.order_by(PartnerOrg.name).limit(200))).scalars()
        return [OrgOut(id=o.id, name=o.name, slug=str(o.slug), status=o.status, verification_status=o.verification_status,
                       verified_at=o.verified_at, verification_expires_at=o.verification_expires_at) for o in rows]  # fmt: skip


@router.get("/verification-requests", response_model=list[VerificationOut])
async def verification_queue(_: PlatformOperatorMfaDep, status_: Annotated[str | None, Query(alias="status")] = "pending") -> list[VerificationOut]:
    return await dev.review_queue(status_ or None)


@router.get("/verification-requests/{request_id}", response_model=VerificationOut)
async def verification_request(request_id: uuid.UUID, _: PlatformOperatorMfaDep) -> VerificationOut:
    return await dev.get_request(request_id)


@router.post("/verification-requests/{request_id}/decide", response_model=VerificationOut)
async def decide(request_id: uuid.UUID, body: DecisionIn, _: PlatformOperatorMfaDep) -> VerificationOut:
    return await dev.decide_verification(get_context(), request_id, body.decision, body.message)


async def _set_status(account_id: uuid.UUID, value: str, operator_id: uuid.UUID) -> None:
    async with platform_session() as db:
        account = await db.get(DeveloperAccount, account_id)
        if account is None:
            raise NotFound("Account not found")
        account.status = value
        audit_log.add(db, f"account.{'locked' if value == 'locked' else 'unlocked'}", account_id=account_id, operator_id=operator_id)


@router.post("/accounts/{account_id}/lock", status_code=status.HTTP_204_NO_CONTENT)
async def lock(account_id: uuid.UUID, op: PlatformOperatorMfaDep) -> None:
    await _set_status(account_id, "locked", op.id)


@router.post("/accounts/{account_id}/unlock", status_code=status.HTTP_204_NO_CONTENT)
async def unlock(account_id: uuid.UUID, op: PlatformOperatorMfaDep) -> None:
    await _set_status(account_id, "active", op.id)


@router.post("/accounts/{account_id}/erase", status_code=status.HTTP_204_NO_CONTENT)
async def erase(account_id: uuid.UUID, op: PlatformOperatorMfaDep) -> None:
    if not await dev.erase_account(account_id, operator_id=op.id):
        raise NotFound("Account not found")
