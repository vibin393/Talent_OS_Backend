# ruff: file-ignore[line-too-long]
"""``/public/v1/users`` (Epic 67, spec A4): read only, scope ``users:read``. Generated shape; see ``services/public_api/directory.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import UserOut
from app.services import public_api_service as svc

router = public_router("users")
Read = scope("users:read")


@router.get("/users", response_model=Page[UserOut])
async def list_users(_: Read, db: PublicDBSessionDep, page: Listing, active: bool | None = None) -> dict[str, Any]:
    return await svc.directory.list_users(db, **page.kw(), active=active)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.directory.get_user(db, user_id)
