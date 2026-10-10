"""Expenses, internal app (Epic 27, FR-137..FR-139): ``/api/v1/expenses``.

Permissions: ``expense:read`` (lists, detail, previews, receipts), ``expense:policy`` (settings and policies),
``expense:admin`` (proxy claims and lines, proxy submit, reopen), ``expense:approve`` (decide;
route membership is
still checked by the approvals engine). Bill amounts and markup are masked without ``compensation:view``.
"""

import uuid
from datetime import date
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.expenses import (
    ClaimDecideIn,
    ClaimDetailOut,
    ClaimSummaryOut,
    ExpenseSettingsOut,
    ExpenseSettingsUpdate,
    LineIn,
    LinePreviewOut,
    PolicyIn,
    PolicyOut,
    PolicyVersionIn,
    ProxyClaimIn,
    ProxySubmitIn,
    ReasonIn,
)
from app.services.expenses import approval, claims, policies, receipts, service
from app.services.expenses.common import get_claim

router = APIRouter(prefix="/expenses", tags=["expenses"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("expense:read"))]
Policy = Annotated[Principal, Depends(require("expense:policy"))]
Admin = Annotated[Principal, Depends(require("expense:admin"))]
Approve = Annotated[Principal, Depends(require("expense:approve"))]


def file_response(data: bytes, filename: str, content_type: str) -> Response:
    ascii_name = (
        "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in filename) or "receipt"
    )
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''"
            + quote(filename, safe=""),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------- settings / policies
@router.get("/settings", response_model=ExpenseSettingsOut)
async def get_settings(_: Policy, db: DB) -> Any:
    return await policies.get_settings(get_context(), db)


@router.patch("/settings", response_model=ExpenseSettingsOut)
async def update_settings(data: ExpenseSettingsUpdate, _: Policy, db: DB) -> Any:
    return await policies.update_settings(get_context(), db, data)


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(_: Read, db: DB) -> Any:
    return await policies.list_policies(get_context(), db)


@router.post("/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(data: PolicyIn, _: Policy, db: DB) -> Any:
    return await policies.create_policy(get_context(), db, data)


@router.get("/policies/{policy_id}", response_model=PolicyOut)
async def get_policy(
    policy_id: uuid.UUID, _: Read, db: DB, version: Annotated[int | None, Query(ge=1)] = None
) -> Any:
    return await policies.get_policy(get_context(), db, policy_id, version)


@router.post("/policies/{policy_id}/versions", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def new_policy_version(policy_id: uuid.UUID, data: PolicyVersionIn, _: Policy, db: DB) -> Any:
    return await policies.new_version(get_context(), db, policy_id, data)


# ---------------------------------------------------------------- claims
@router.get("/claims", response_model=list[ClaimSummaryOut])
async def list_claims(
    _: Read,
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    placement_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    flag: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    unposted: bool = False,
) -> Any:
    return await service.list_claims(
        get_context(),
        db,
        status=status_,
        placement_id=placement_id,
        client_id=client_id,
        candidate_id=candidate_id,
        flag=flag,
        date_from=date_from,
        date_to=date_to,
        unposted=unposted,
    )


@router.post("/claims", response_model=ClaimDetailOut, status_code=status.HTTP_201_CREATED)
async def create_proxy_claim(data: ProxyClaimIn, _: Admin, db: DB) -> Any:
    ctx = get_context()
    claim = await claims.create_claim(
        ctx, db, data.placement_id, title=data.title, proxy_reason=data.proxy_reason
    )
    return await claims.detail(ctx, db, claim)


@router.get("/claims/{claim_id}", response_model=ClaimDetailOut)
async def get_claim_detail(claim_id: uuid.UUID, _: Read, db: DB) -> Any:
    ctx = get_context()
    return await claims.detail(ctx, db, await get_claim(db, claim_id))


@router.post("/claims/{claim_id}/lines/preview", response_model=LinePreviewOut)
async def preview_line(claim_id: uuid.UUID, data: LineIn, _: Read, db: DB) -> Any:
    return await claims.preview_line(get_context(), db, await get_claim(db, claim_id), data)


@router.post("/claims/{claim_id}/lines", response_model=ClaimDetailOut)
async def add_line(claim_id: uuid.UUID, data: LineIn, _: Admin, db: DB) -> Any:
    ctx = get_context()
    claim = await get_claim(db, claim_id, lock=True)
    await claims.upsert_line(ctx, db, claim, data)
    return await claims.detail(ctx, db, claim)


@router.put("/claims/{claim_id}/lines/{line_id}", response_model=ClaimDetailOut)
async def update_line(claim_id: uuid.UUID, line_id: uuid.UUID, data: LineIn, _: Admin, db: DB) -> Any:
    ctx = get_context()
    claim = await get_claim(db, claim_id, lock=True)
    await claims.upsert_line(ctx, db, claim, data, line_id)
    return await claims.detail(ctx, db, claim)


@router.delete("/claims/{claim_id}/lines/{line_id}", response_model=ClaimDetailOut)
async def delete_line(claim_id: uuid.UUID, line_id: uuid.UUID, _: Admin, db: DB) -> Any:
    ctx = get_context()
    claim = await get_claim(db, claim_id, lock=True)
    await claims.delete_line(ctx, db, claim, line_id)
    return await claims.detail(ctx, db, claim)


@router.post("/claims/{claim_id}/submit", response_model=ClaimDetailOut)
async def proxy_submit(claim_id: uuid.UUID, data: ProxySubmitIn, _: Admin, db: DB) -> Any:
    ctx = get_context()
    claim = await get_claim(db, claim_id, lock=True)
    await claims.submit(ctx, db, claim, proxy_reason=data.proxy_reason)
    return await claims.detail(ctx, db, claim)


@router.post("/claims/{claim_id}/decide", response_model=ClaimDetailOut)
async def decide(claim_id: uuid.UUID, data: ClaimDecideIn, _: Approve, db: DB) -> Any:
    ctx = get_context()
    claim = await get_claim(db, claim_id, lock=True)
    payload = [d.model_dump(mode="json") for d in data.line_decisions]
    await approval.decide_as(ctx, db, claim, data.action, data.comment, payload)
    await db.refresh(claim)
    return await claims.detail(ctx, db, claim)


@router.post("/claims/{claim_id}/reopen", response_model=ClaimDetailOut)
async def reopen(claim_id: uuid.UUID, data: ReasonIn, _: Admin, db: DB) -> Any:
    ctx = get_context()
    claim = await get_claim(db, claim_id, lock=True)
    await claims.reopen(ctx, db, claim, data.reason)
    return await claims.detail(ctx, db, claim)


@router.get(
    "/claims/{claim_id}/lines/{line_id}/receipt",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def line_receipt(claim_id: uuid.UUID, line_id: uuid.UUID, _: Read, db: DB) -> Response:
    data, name, ctype = await receipts.line_receipt(db, claim_id, line_id)
    return file_response(data, name, ctype)
