# ruff: noqa: E501
"""``/public/v1/postings`` (Epic 67, spec A4): careers-site postings; publish and close (job boards belong to Epic 70)."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import PostingOut
from app.services import public_api_service as svc

router = public_router("postings")
Read = scope("postings:read")
Write = scope("postings:write")


@router.get("/postings", response_model=Page[PostingOut])
async def list_postings(_: Read, db: PublicDBSessionDep, page: Listing, is_live: bool | None = None) -> dict[str, Any]:
    return await svc.demands.list_postings(db, **page.kw(), is_live=is_live)


@router.get("/postings/{posting_id}", response_model=PostingOut)
async def get_posting(posting_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.demands.get_posting(db, posting_id)


@router.post("/postings/{posting_id}/publish", response_model=PostingOut)
async def publish_posting(posting_id: uuid.UUID, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.demands.publish_posting(db, posting_id))


@router.post("/postings/{posting_id}/close", response_model=PostingOut)
async def close_posting(posting_id: uuid.UUID, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.demands.close_posting(db, posting_id))
