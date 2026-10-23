# ruff: file-ignore[line-too-long]
"""``/public/v1/notes`` (Epic 67, spec A4): notes on records the caller can read."""

import uuid
from typing import Annotated, Any

from fastapi import Query, status

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import NoteCreateIn, NoteOut
from app.services import public_api_service as svc

router = public_router("notes")


@router.get("/notes", response_model=Page[NoteOut])
async def list_notes(
    _: scope("notes:read"), db: PublicDBSessionDep, page: Listing, entity_type: Annotated[str, Query(max_length=30)],
    entity_id: uuid.UUID,
) -> dict[str, Any]:  # fmt: skip
    return await svc.engagement.list_entries(db, kinds=("note",), entity_type=entity_type, entity_id=entity_id, **page.kw())


@router.post("/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def create_note(body: NoteCreateIn, _: scope("notes:write"), db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.engagement.create_note(db, body))
