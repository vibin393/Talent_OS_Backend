"""Search endpoints (Epic 5): ``POST /api/v1/search/candidates`` and ``GET /api/v1/search/global``.
Each endpoint calls one service function (``app.services.search``)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.context import get_context
from app.core.permissions import Principal, current_principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.search import CandidateSearchIn, CandidateSearchOut, GlobalSearchOut
from app.services import search as svc

router = APIRouter(prefix="/search", tags=["search"])

Read = Annotated[Principal, Depends(require("candidate:read"))]
Authenticated = Annotated[Principal, Depends(current_principal)]
DB = TenantDBSessionDep


@router.post("/candidates", response_model=CandidateSearchOut)
async def search_candidates(body: CandidateSearchIn, _: Read, db: DB) -> Any:
    return await svc.search_candidates(get_context(), db, body)


@router.get("/global", response_model=GlobalSearchOut)
async def global_search(
    _: Authenticated,
    db: DB,
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> Any:
    return await svc.run_global_search(get_context(), db, q, limit)
