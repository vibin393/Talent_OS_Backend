"""Approval endpoints (story 4.2): chains (``settings:manage``), My approvals, request detail, decisions.
Each endpoint calls one service function."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.approvals import ChainIn, ChainOut, DecideIn, RequestOut
from app.services import approvals as svc

chains_router = APIRouter(prefix="/approval-chains", tags=["approvals"])
router = APIRouter(prefix="/approvals", tags=["approvals"])

SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


@chains_router.get("", response_model=list[ChainOut])
async def list_chains(_: SettingsManage, db: DB) -> Any:
    return await svc.list_chains(get_context(), db)


@chains_router.post("", response_model=ChainOut, status_code=status.HTTP_201_CREATED)
async def create_chain(body: ChainIn, _: SettingsManage, db: DB) -> Any:
    return await svc.create_chain(get_context(), db, body)


@chains_router.get("/{chain_id}", response_model=ChainOut)
async def get_chain(chain_id: uuid.UUID, _: SettingsManage, db: DB) -> Any:
    return await svc.get_chain(get_context(), db, chain_id)


@chains_router.put("/{chain_id}", response_model=ChainOut)
async def update_chain(chain_id: uuid.UUID, body: ChainIn, _: SettingsManage, db: DB) -> Any:
    return await svc.update_chain(get_context(), db, chain_id, body)


@chains_router.delete("/{chain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chain(chain_id: uuid.UUID, _: SettingsManage, db: DB) -> Response:
    await svc.delete_chain(get_context(), db, chain_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/mine", response_model=Page[RequestOut])
async def my_approvals(_: CurrentUserDep, db: DB, params: Paging) -> Any:
    return await svc.list_my_pending(get_context(), db, params)


@router.get("/{request_id}", response_model=RequestOut)
async def get_request(request_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.get_request(get_context(), db, request_id)


@router.post("/{request_id}/decide", response_model=RequestOut)
async def decide(request_id: uuid.UUID, body: DecideIn, _: CurrentUserDep, db: DB) -> Any:
    return await svc.decide(get_context(), db, request_id, body)
