# ruff: file-ignore[line-too-long]
"""``/public/v1/placements`` (Epic 67, spec A4): read only, scope ``placements:read``. Generated shape; see ``services/public_api/delivery.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import PlacementOut
from app.services import public_api_service as svc

router = public_router("placements")
Read = scope("placements:read")


@router.get("/placements", response_model=Page[PlacementOut])
async def list_placements(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.delivery.list_placements(db, **page.kw())


@router.get("/placements/{placement_id}", response_model=PlacementOut)
async def get_placement(placement_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.delivery.get_placement(db, placement_id)
