# ruff: file-ignore[line-too-long]
"""``/public/v1/workers`` (Epic 67, spec A4): read only, scope ``workers:read``. Generated shape; see ``services/public_api/delivery.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import WorkerOut
from app.services import public_api_service as svc

router = public_router("workers")
Read = scope("workers:read")


@router.get("/workers", response_model=Page[WorkerOut])
async def list_workers(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.delivery.list_workers(db, **page.kw())


@router.get("/workers/{worker_id}", response_model=WorkerOut)
async def get_worker(worker_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.delivery.get_worker(db, worker_id)
