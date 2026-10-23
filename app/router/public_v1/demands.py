# ruff: file-ignore[line-too-long]
"""``/public/v1/demands`` (Epic 67, spec A4): reads and draft create only (no submit / approve / open)."""

import uuid
from typing import Annotated, Any

from fastapi import Query, status

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import DemandCreateIn, DemandOut
from app.services import public_api_service as svc

router = public_router("demands")
Read = scope("demands:read")
Write = scope("demands:write")


@router.get("/demands", response_model=Page[DemandOut])
async def list_demands(
    _: Read, db: PublicDBSessionDep, page: Listing, status_: Annotated[str | None, Query(alias="status", max_length=30)] = None,
    client_id: uuid.UUID | None = None,
) -> dict[str, Any]:  # fmt: skip
    return await svc.demands.list_demands(db, **page.kw(), status=status_, client_id=client_id)


@router.get("/demands/{demand_id}", response_model=DemandOut)
async def get_demand(demand_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.demands.get_demand(db, demand_id)


@router.post("/demands", response_model=DemandOut, status_code=status.HTTP_201_CREATED)
async def create_demand(body: DemandCreateIn, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.demands.create_demand(db, body))
