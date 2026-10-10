# ruff: noqa: E501
"""``/public/v1/offers`` (Epic 67, spec A4): read only, scope ``offers:read``. Generated shape; see ``services/public_api/delivery.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import OfferOut
from app.services import public_api_service as svc

router = public_router("offers")
Read = scope("offers:read")


@router.get("/offers", response_model=Page[OfferOut])
async def list_offers(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.delivery.list_offers(db, **page.kw())


@router.get("/offers/{offer_id}", response_model=OfferOut)
async def get_offer(offer_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.delivery.get_offer(db, offer_id)
