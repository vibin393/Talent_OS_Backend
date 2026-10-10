# ruff: noqa: E501
"""Knowledge contract DTOs (Epic 53 contract layer, spec 0.12; filled by Epic 62). Frozen; field sets pinned by
``tests/contracts/signatures.json``."""

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: uuid.UUID
    source_version: int
    title: str
    excerpt: str
    citation: str
    score: Decimal


__all__ = ["KnowledgeHit"]


# ================================================================ Epic 62: Knowledge Sources (internal API)
# (``KnowledgeHit`` above stays frozen; the DTOs below are the Knowledge Sources admin and Onboarding Q&A internals.)
from datetime import date as _date  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402
from typing import Literal as _Literal  # noqa: E402

from pydantic import Field as _Field  # noqa: E402

SourceKind = _Literal["article", "file", "onboarding_template", "policy_document"]
Audience = _Literal["worker", "candidate", "both", "internal"]
SourceStatus = _Literal["draft", "published", "unpublished", "archived"]
Topic = _Literal["pay", "tax", "legal", "immigration", "documents", "policy", "it", "other"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceCreate(_In):
    title: str = _Field(min_length=1, max_length=200)
    kind: _Literal["article", "policy_document"] = "article"
    audience: Audience
    body_md: str | None = _Field(default=None, max_length=100_000)
    tags: list[str] = _Field(default_factory=list, max_length=20)
    legal_entity_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    last_reviewed_on: _date | None = None


class SourceUpdate(_In):
    title: str | None = _Field(default=None, min_length=1, max_length=200)
    audience: Audience | None = None
    tags: list[str] | None = _Field(default=None, max_length=20)
    legal_entity_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    last_reviewed_on: _date | None = None


class VersionCreate(_In):
    body_md: str = _Field(min_length=1, max_length=100_000)


class PublishIn(_In):
    version_no: int = _Field(ge=1)


class SearchPreviewIn(_In):
    query: str = _Field(min_length=1, max_length=500)
    audience: _Literal["worker", "candidate", "internal"] = "worker"


class SourceOut(BaseModel):
    id: uuid.UUID
    title: str
    kind: SourceKind
    audience: Audience
    status: SourceStatus
    current_version_no: int
    published_version_no: int | None
    ref_kind: str | None
    ref_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    client_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    tags: list[str]
    last_reviewed_on: _date | None
    created_at: _datetime
    updated_at: _datetime


class VersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    body_md: str | None
    file_id: uuid.UUID | None
    extract_status: str
    extracted_chars: int
    chunk_count: int
    published: bool
    created_by: uuid.UUID | None
    created_at: _datetime


class SourceDetailOut(SourceOut):
    current_body_md: str | None = None
    versions: list[VersionOut] = _Field(default_factory=list)


class ImportResultOut(BaseModel):
    created: int
    skipped: int
    source_ids: list[uuid.UUID]


class SearchHitOut(BaseModel):
    source_id: uuid.UUID
    source_version: int
    title: str
    excerpt: str
    citation: str
    score: float


class GapRow(BaseModel):
    question: str  # Epic 78: the decrypted representative for knowledge:manage holders, else the topic label
    topic: Topic | None
    topic_label: str | None = None  # Epic 78 (FR-404): the non-reversible cluster label
    times_asked: int
    outcome: _Literal["handed_off", "ungrounded", "incorrect"]
    last_asked: _datetime
    thread_ids: list[uuid.UUID]


class QaCitation(BaseModel):
    citation: str
    title: str
    kind: _Literal["knowledge", "task", "case"]


class QaMessageOut(BaseModel):
    id: uuid.UUID
    seq: int
    role: _Literal["asker", "assistant", "coordinator", "system"]
    text: str
    citations: list[QaCitation]
    outcome: str | None
    topic: Topic | None
    feedback: str | None
    agent_run_id: uuid.UUID | None
    author_user_id: uuid.UUID | None
    created_at: _datetime


class QaThreadOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str
    audience: _Literal["worker", "candidate"]
    status: _Literal["open", "answered", "handed_off", "closed"]
    coordinator_user_id: uuid.UUID | None
    coordinator_name: str | None
    placement_id: uuid.UUID | None
    task_id: uuid.UUID | None
    last_message_at: _datetime | None
    created_at: _datetime
    last_question: str | None = None
    messages: list[QaMessageOut] = _Field(default_factory=list)


class CoordinatorReplyIn(_In):
    text: str = _Field(min_length=1, max_length=1500)


__all__ += [
    "Audience",
    "CoordinatorReplyIn",
    "GapRow",
    "ImportResultOut",
    "PublishIn",
    "QaCitation",
    "QaMessageOut",
    "QaThreadOut",
    "SearchHitOut",
    "SearchPreviewIn",
    "SourceCreate",
    "SourceDetailOut",
    "SourceOut",
    "SourceUpdate",
    "Topic",
    "VersionCreate",
    "VersionOut",
]
