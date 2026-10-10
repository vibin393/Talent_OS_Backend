"""Candidate database endpoints (Epic 3): ``/api/v1/candidates``, ``/skills``, ``/tags``,
``/candidate-lists``. Each endpoint calls one service function (``app.services.candidates``)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.ai import AIActionOut
from app.schemas.candidates import (
    BulkTagsIn,
    BulkTagsOut,
    Candidate360Out,
    CandidateCreate,
    CandidateCreatedOut,
    CandidateFilters,
    CandidateListCreate,
    CandidateListOut,
    CandidateListUpdate,
    CandidateOut,
    CandidateSkillOut,
    CandidateSkillsSet,
    CandidateUpdate,
    DuplicateDetailOut,
    DuplicateOut,
    DuplicateStatus,
    ImportBatchDetailOut,
    ImportBatchOut,
    ImportItemStatus,
    ListMembersIn,
    ListSharesIn,
    MergeIn,
    MergeOut,
    PendingSkillOut,
    ResumeApply,
    ResumeApplyOut,
    ResumeDraftOut,
    ResumeOut,
    ResumeUploadOut,
    SkillAliasCreate,
    SkillCreate,
    SkillOut,
    SkillReject,
    TagNamesIn,
    TagOut,
)
from app.services import candidates as svc
from app.services import files_service
from app.services.talent_pools import legacy_lists

router = APIRouter(prefix="/candidates", tags=["candidates"])
skills_router = APIRouter(prefix="/skills", tags=["skills"])
tags_router = APIRouter(prefix="/tags", tags=["candidates"])
lists_router = APIRouter(prefix="/candidate-lists", tags=["candidate-lists"])

Read = Annotated[Principal, Depends(require("candidate:read"))]
Create = Annotated[Principal, Depends(require("candidate:create"))]
Update = Annotated[Principal, Depends(require("candidate:update"))]
Delete = Annotated[Principal, Depends(require("candidate:delete"))]
Merge = Annotated[Principal, Depends(require("candidate:merge"))]
SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


# ================================================================ collection
@router.get("", response_model=Page[CandidateOut])
async def list_candidates(
    _: Read, db: DB, filters: Annotated[CandidateFilters, Query()], params: Paging
) -> Page[CandidateOut]:
    return await svc.records.list_candidates(get_context(), db, filters, params)


@router.post("", response_model=CandidateCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_candidate(body: CandidateCreate, _: Create, db: DB) -> Any:
    return await svc.records.create_candidate(get_context(), db, body)


@router.post("/resume-drafts", response_model=ResumeDraftOut, status_code=status.HTTP_201_CREATED)
async def create_resume_draft(_: Create, db: DB, file: Annotated[UploadFile, File()]) -> ResumeDraftOut:
    data = await files_service.read_upload(file)
    return await svc.parsing.create_resume_draft(get_context(), db, data, file.filename)


# ================================================================ duplicates / merges
@router.get("/duplicates", response_model=Page[DuplicateOut])
async def list_duplicates(
    _: Read, db: DB, params: Paging, status_: Annotated[DuplicateStatus | None, Query(alias="status")] = None
) -> Page[DuplicateOut]:
    return await svc.duplicates.list_duplicates(get_context(), db, status_, params)


@router.get("/duplicates/{duplicate_id}", response_model=DuplicateDetailOut)
async def get_duplicate(duplicate_id: uuid.UUID, _: Read, db: DB) -> DuplicateDetailOut:
    return await svc.duplicates.get_duplicate(get_context(), db, duplicate_id)


@router.post("/duplicates/{duplicate_id}/merge", response_model=MergeOut)
async def merge_duplicate(duplicate_id: uuid.UUID, body: MergeIn, _: Merge, db: DB) -> MergeOut:
    return await svc.merge.merge(get_context(), db, duplicate_id, body)


@router.post("/duplicates/{duplicate_id}/keep-separate", response_model=DuplicateOut)
async def keep_separate(duplicate_id: uuid.UUID, _: Merge, db: DB) -> DuplicateOut:
    return await svc.duplicates.keep_separate(get_context(), db, duplicate_id)


@router.get("/merges", response_model=Page[MergeOut])
async def list_merges(_: Read, db: DB, params: Paging) -> Page[MergeOut]:
    return await svc.merge.list_merges(get_context(), db, params)


@router.post("/merges/{merge_id}/undo", response_model=MergeOut)
async def undo_merge(merge_id: uuid.UUID, _: Merge, db: DB) -> MergeOut:
    return await svc.merge.undo(get_context(), db, merge_id)


# ================================================================ bulk import
@router.post("/imports", response_model=ImportBatchOut, status_code=status.HTTP_201_CREATED)
async def create_import(
    _: Create,
    db: DB,
    files: Annotated[list[UploadFile] | None, File()] = None,
    files_brackets: Annotated[list[UploadFile] | None, File(alias="files[]")] = None,
) -> ImportBatchOut:
    uploads = await svc.imports.read_uploads([*(files or []), *(files_brackets or [])])
    return await svc.imports.create_import(get_context(), db, uploads)


@router.get("/imports", response_model=Page[ImportBatchOut])
async def list_imports(_: Read, db: DB, params: Paging) -> Page[ImportBatchOut]:
    return await svc.imports.list_imports(get_context(), db, params)


@router.get("/imports/{batch_id}", response_model=ImportBatchDetailOut)
async def get_import(
    batch_id: uuid.UUID,
    _: Read,
    db: DB,
    params: Paging,
    status_: Annotated[ImportItemStatus | None, Query(alias="status")] = None,
) -> ImportBatchDetailOut:
    return await svc.imports.get_import(get_context(), db, batch_id, params, status_)


# ================================================================ bulk tags
@router.post("/bulk/tags", response_model=BulkTagsOut)
async def bulk_tags(body: BulkTagsIn, _: Update, db: DB) -> BulkTagsOut:
    return await svc.tags_lists.bulk_tags(get_context(), db, body)


# ================================================================ one candidate
@router.get("/{candidate_id}", response_model=Candidate360Out)
async def get_candidate(candidate_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.records.get_candidate_360(get_context(), db, candidate_id)


@router.patch("/{candidate_id}", response_model=Candidate360Out)
async def update_candidate(candidate_id: uuid.UUID, body: CandidateUpdate, _: Update, db: DB) -> Any:
    return await svc.records.update_candidate(get_context(), db, candidate_id, body)


@router.put("/{candidate_id}/skills", response_model=list[CandidateSkillOut])
async def set_skills(
    candidate_id: uuid.UUID, body: CandidateSkillsSet, _: Update, db: DB
) -> list[CandidateSkillOut]:
    return await svc.records.set_skills(get_context(), db, candidate_id, body)


@router.post("/{candidate_id}/archive", response_model=Candidate360Out)
async def archive_candidate(candidate_id: uuid.UUID, _: Delete, db: DB) -> Any:
    return await svc.records.archive_candidate(get_context(), db, candidate_id)


@router.post("/{candidate_id}/unarchive", response_model=Candidate360Out)
async def unarchive_candidate(candidate_id: uuid.UUID, _: Delete, db: DB) -> Any:
    return await svc.records.unarchive_candidate(get_context(), db, candidate_id)


@router.get("/{candidate_id}/resumes", response_model=list[ResumeOut])
async def list_resumes(candidate_id: uuid.UUID, _: Read, db: DB) -> list[ResumeOut]:
    return await svc.parsing.list_resumes(get_context(), db, candidate_id)


@router.post("/{candidate_id}/resumes", response_model=ResumeUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    candidate_id: uuid.UUID, _: Update, db: DB, file: Annotated[UploadFile, File()]
) -> ResumeUploadOut:
    data = await files_service.read_upload(file)
    return await svc.parsing.upload_resume(get_context(), db, candidate_id, data, file.filename)


@router.post("/{candidate_id}/resumes/{resume_id}/apply", response_model=ResumeApplyOut)
async def apply_resume(
    candidate_id: uuid.UUID, resume_id: uuid.UUID, body: ResumeApply, _: Update, db: DB
) -> Any:
    return await svc.parsing.apply_resume(get_context(), db, candidate_id, resume_id, body)


@router.get("/{candidate_id}/ai-actions", response_model=list[AIActionOut])
async def list_ai_actions(candidate_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.parsing.list_ai_actions(get_context(), db, candidate_id)


@router.post("/{candidate_id}/tags", response_model=list[TagOut])
async def add_tags(candidate_id: uuid.UUID, body: TagNamesIn, _: Update, db: DB) -> list[TagOut]:
    return await svc.tags_lists.add_tags(get_context(), db, candidate_id, body)


@router.delete("/{candidate_id}/tags/{tag_id}", response_model=list[TagOut])
async def remove_tag(candidate_id: uuid.UUID, tag_id: uuid.UUID, _: Update, db: DB) -> list[TagOut]:
    return await svc.tags_lists.remove_tag(get_context(), db, candidate_id, tag_id)


# ================================================================ skills catalog
@skills_router.get("", response_model=Page[SkillOut])
async def search_skills(
    _: Read,
    db: DB,
    params: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_pending: bool = False,
) -> Page[SkillOut]:
    return await svc.skills.search_skills(
        get_context(), db, q, include_pending=include_pending, params=params
    )


@skills_router.post("", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def create_skill(body: SkillCreate, _: SettingsManage, db: DB) -> SkillOut:
    return await svc.skills.create_skill(get_context(), db, body)


@skills_router.get("/pending", response_model=Page[PendingSkillOut])
async def list_pending_skills(_: SettingsManage, db: DB, params: Paging) -> Page[PendingSkillOut]:
    return await svc.skills.list_pending(get_context(), db, params)


@skills_router.post(
    "/{kind}/{skill_id}/aliases", response_model=SkillOut, status_code=status.HTTP_201_CREATED
)
async def add_skill_alias(
    kind: str, skill_id: uuid.UUID, body: SkillAliasCreate, _: SettingsManage, db: DB
) -> SkillOut:
    return await svc.skills.add_alias(get_context(), db, kind, skill_id, body.alias)


@skills_router.post("/{skill_id}/approve", response_model=SkillOut)
async def approve_skill(skill_id: uuid.UUID, _: SettingsManage, db: DB) -> SkillOut:
    return await svc.skills.approve_skill(get_context(), db, skill_id)


@skills_router.post("/{skill_id}/reject", response_model=SkillOut)
async def reject_skill(
    skill_id: uuid.UUID, _: SettingsManage, db: DB, body: SkillReject | None = None
) -> SkillOut:
    return await svc.skills.reject_skill(get_context(), db, skill_id, body.merge_into if body else None)


# ================================================================ tags
@tags_router.get("", response_model=Page[TagOut])
async def list_tags(
    _: Read, db: DB, params: Paging, q: Annotated[str | None, Query(max_length=100)] = None
) -> Page[TagOut]:
    return await svc.tags_lists.list_tags(get_context(), db, q, params)


# ================================================================ saved lists
# Epic 38 (decision 11): P1 saved lists are static Talent Pools now; these endpoints are aliases that keep their P1
# request / response schemas (``talent_pools.legacy_lists`` writes the Pool and mirrors it to the P1 tables).
@lists_router.get("", response_model=Page[CandidateListOut])
async def list_lists(
    _: Read, db: DB, params: Paging, include_archived: bool = False
) -> Page[CandidateListOut]:
    return await legacy_lists.list_lists(get_context(), db, params, include_archived=include_archived)


@lists_router.post("", response_model=CandidateListOut, status_code=status.HTTP_201_CREATED)
async def create_list(body: CandidateListCreate, _: Read, db: DB) -> CandidateListOut:
    return await legacy_lists.create_list(get_context(), db, body)


@lists_router.get("/{list_id}", response_model=CandidateListOut)
async def get_list(list_id: uuid.UUID, _: Read, db: DB) -> CandidateListOut:
    return await legacy_lists.get_list(get_context(), db, list_id)


@lists_router.patch("/{list_id}", response_model=CandidateListOut)
async def update_list(list_id: uuid.UUID, body: CandidateListUpdate, _: Read, db: DB) -> CandidateListOut:
    return await legacy_lists.update_list(get_context(), db, list_id, body)


@lists_router.delete("/{list_id}", response_model=CandidateListOut)
async def archive_list(list_id: uuid.UUID, _: Read, db: DB) -> CandidateListOut:
    return await legacy_lists.archive_list(get_context(), db, list_id)


@lists_router.put("/{list_id}/shares", response_model=CandidateListOut)
async def set_list_shares(list_id: uuid.UUID, body: ListSharesIn, _: Read, db: DB) -> CandidateListOut:
    return await legacy_lists.set_shares(get_context(), db, list_id, body)


@lists_router.post("/{list_id}/members", response_model=CandidateListOut)
async def add_list_members(list_id: uuid.UUID, body: ListMembersIn, _: Read, db: DB) -> CandidateListOut:
    return await legacy_lists.add_members(get_context(), db, list_id, body)


@lists_router.delete("/{list_id}/members/{candidate_id}", response_model=CandidateListOut)
async def remove_list_member(
    list_id: uuid.UUID, candidate_id: uuid.UUID, _: Read, db: DB
) -> CandidateListOut:
    return await legacy_lists.remove_member(get_context(), db, list_id, candidate_id)
