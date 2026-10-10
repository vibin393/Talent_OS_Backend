# ruff: noqa: E501
"""``/public/v1/skills`` (Epic 67, spec A4): the skills catalogue (``skills:read``)."""

from typing import Annotated, Any

from fastapi import Query

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import SkillOut
from app.services import public_api_service as svc

router = public_router("skills")


@router.get("/skills", response_model=Page[SkillOut])
async def list_skills(_: scope("skills:read"), db: PublicDBSessionDep, page: Listing, q: Annotated[str | None, Query(max_length=100)] = None) -> dict[str, Any]:
    return await svc.directory.list_skills(db, **page.kw(), q=q)
