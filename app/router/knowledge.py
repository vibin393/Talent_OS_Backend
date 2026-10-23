# ruff: file-ignore[line-too-long]
"""Knowledge Sources admin, FAQ gaps and the coordinators' onboarding question inbox (Epic 62 A8). Internal principals;
API keys may read only (writes are human-only).

| Method + path | Permission |
|---|---|
| ``GET /knowledge/sources`` · ``GET /knowledge/sources/{id}`` · ``GET /knowledge/sources/{id}/versions`` | ``knowledge:read`` |
| ``POST /knowledge/sources`` (+ ``/upload``) · ``PATCH /knowledge/sources/{id}`` · ``POST .../versions`` (+ ``/upload``) · ``POST .../publish|unpublish|archive`` · ``POST /knowledge/import-onboarding`` · ``POST /knowledge/search-preview`` | ``knowledge:manage`` |
| ``GET /knowledge/gaps`` | ``knowledge:manage`` or ``onboarding:manage`` |
| ``GET /onboarding-qa/threads`` · ``GET /onboarding-qa/threads/{id}`` | ``onboarding:read`` |
| ``POST /onboarding-qa/threads/{id}/reply`` · ``POST /onboarding-qa/threads/{id}/close`` | ``onboarding:manage`` |
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.knowledge import (
    Audience,
    CoordinatorReplyIn,
    GapRow,
    ImportResultOut,
    PublishIn,
    QaThreadOut,
    SearchHitOut,
    SearchPreviewIn,
    SourceCreate,
    SourceDetailOut,
    SourceOut,
    SourceUpdate,
    VersionCreate,
    VersionOut,
)
from app.services import knowledge_service as svc

router = APIRouter(tags=["knowledge"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("knowledge:read"))]
Manage = Annotated[Principal, Depends(require("knowledge:manage"))]
Gaps = Annotated[Principal, Depends(require_any("knowledge:manage", "onboarding:manage"))]
QaRead = Annotated[Principal, Depends(require("onboarding:read"))]
QaManage = Annotated[Principal, Depends(require("onboarding:manage"))]
SOURCES = "/knowledge/sources"


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


async def _read(file: UploadFile) -> tuple[bytes, str | None]:
    from app.services import files_service

    return await files_service.read_upload(file, 10), file.filename


@router.get(SOURCES, response_model=list[SourceOut])
async def list_sources(
    _: Read, db: DB, status_: Annotated[str | None, Query(alias="status")] = None,
    audience: Audience | None = None, q: Annotated[str | None, Query(max_length=100)] = None,
) -> list[SourceOut]:  # fmt: skip
    return await svc.sources.list_sources(get_context(), db, status=status_, audience=audience, q=q)


@router.post(SOURCES, response_model=SourceDetailOut, status_code=status.HTTP_201_CREATED)
async def create_source(data: SourceCreate, _: Manage, db: DB) -> SourceDetailOut:
    _human()
    return await svc.sources.create_source(get_context(), db, data)


@router.post(f"{SOURCES}/upload", response_model=SourceDetailOut, status_code=status.HTTP_201_CREATED)
async def upload_source(
    _: Manage, db: DB, title: Annotated[str, Form(min_length=1, max_length=200)],
    audience: Annotated[Literal["worker", "candidate", "both", "internal"], Form()], file: Annotated[UploadFile, File()],
) -> SourceDetailOut:  # fmt: skip
    _human()
    ctx = get_context()
    created = await svc.sources.create_source(
        ctx, db, SourceCreate(title=title, kind="article", audience=audience)
    )
    await svc.sources.new_version(ctx, db, created.id, file=await _read(file))
    return await svc.sources.get_source(ctx, db, created.id)


@router.get(SOURCES + "/{source_id}", response_model=SourceDetailOut)
async def get_source(source_id: uuid.UUID, _: Read, db: DB) -> SourceDetailOut:
    return await svc.sources.get_source(get_context(), db, source_id)


@router.patch(SOURCES + "/{source_id}", response_model=SourceDetailOut)
async def update_source(source_id: uuid.UUID, data: SourceUpdate, _: Manage, db: DB) -> SourceDetailOut:
    _human()
    return await svc.sources.update_source(get_context(), db, source_id, data)


@router.get(SOURCES + "/{source_id}/versions", response_model=list[VersionOut])
async def list_versions(source_id: uuid.UUID, _: Read, db: DB) -> list[VersionOut]:
    return await svc.sources.list_versions(get_context(), db, source_id)


@router.post(
    SOURCES + "/{source_id}/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED
)
async def new_version(source_id: uuid.UUID, data: VersionCreate, _: Manage, db: DB) -> VersionOut:
    _human()
    return await svc.sources.new_version(get_context(), db, source_id, body_md=data.body_md)


@router.post(
    SOURCES + "/{source_id}/versions/upload", response_model=VersionOut, status_code=status.HTTP_201_CREATED
)
async def upload_version(
    source_id: uuid.UUID, _: Manage, db: DB, file: Annotated[UploadFile, File()]
) -> VersionOut:
    _human()
    return await svc.sources.new_version(get_context(), db, source_id, file=await _read(file))


@router.post(SOURCES + "/{source_id}/publish", response_model=SourceDetailOut)
async def publish(source_id: uuid.UUID, data: PublishIn, _: Manage, db: DB) -> SourceDetailOut:
    _human()
    return await svc.sources.publish(get_context(), db, source_id, data.version_no)


@router.post(SOURCES + "/{source_id}/unpublish", response_model=SourceDetailOut)
async def unpublish(source_id: uuid.UUID, _: Manage, db: DB) -> SourceDetailOut:
    _human()
    return await svc.sources.unpublish(get_context(), db, source_id)


@router.post(SOURCES + "/{source_id}/archive", response_model=SourceDetailOut)
async def archive(source_id: uuid.UUID, _: Manage, db: DB) -> SourceDetailOut:
    _human()
    return await svc.sources.archive(get_context(), db, source_id)


@router.post("/knowledge/import-onboarding", response_model=ImportResultOut)
async def import_onboarding(_: Manage, db: DB) -> ImportResultOut:
    _human()
    return await svc.sources.import_onboarding(get_context(), db)


@router.post("/knowledge/search-preview", response_model=list[SearchHitOut])
async def search_preview(data: SearchPreviewIn, principal: Manage, db: DB) -> list[SearchHitOut]:
    import dataclasses

    from app.core.permissions import Scope

    ctx = get_context()
    if not principal.has("knowledge:read"):  # managers can always test what the assistant would see
        ctx = dataclasses.replace(
            ctx,
            principal=dataclasses.replace(
                principal, permissions={**principal.permissions, "knowledge:read": Scope.TENANT}
            ),
        )
    hits = await svc.search(ctx, db, data.query, audience=data.audience, limit=5)
    return [
        SearchHitOut(
            source_id=h.source_id,
            source_version=h.source_version,
            title=h.title,
            excerpt=h.excerpt,
            citation=h.citation,
            score=float(h.score),
        )
        for h in hits
    ]


@router.get("/knowledge/gaps", response_model=list[GapRow])
async def gaps(_: Gaps, db: DB, topic: str | None = None) -> list[GapRow]:
    return await svc.gaps.gap_report(get_context(), db, topic=topic)


@router.get("/onboarding-qa/threads", response_model=list[QaThreadOut])
async def list_threads(
    _: QaRead, db: DB, view: Literal["waiting", "mine", "answered", "all"] = "all"
) -> list[QaThreadOut]:
    return await svc.qa.list_internal(get_context(), db, view=view)


@router.get("/onboarding-qa/threads/{thread_id}", response_model=QaThreadOut)
async def get_thread(thread_id: uuid.UUID, _: QaRead, db: DB) -> QaThreadOut:
    return await svc.qa.get_internal(get_context(), db, thread_id)


@router.post("/onboarding-qa/threads/{thread_id}/reply", response_model=QaThreadOut)
async def reply(thread_id: uuid.UUID, data: CoordinatorReplyIn, _: QaManage, db: DB) -> QaThreadOut:
    _human()
    return await svc.qa.coordinator_reply(get_context(), db, thread_id, data.text)


@router.post("/onboarding-qa/threads/{thread_id}/close", response_model=QaThreadOut)
async def close(thread_id: uuid.UUID, _: QaManage, db: DB) -> QaThreadOut:
    _human()
    return await svc.qa.close(get_context(), db, thread_id)
