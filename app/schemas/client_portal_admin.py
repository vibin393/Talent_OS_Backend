"""Internal (tenant user) schemas of the Client Portal epic (Epic 18): recruiter slot offers and the Client 360
compliance packs. Never returned to a portal principal."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.pipeline_portal import SlotIn


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SlotOfferIn(_In):
    slots: list[SlotIn] = Field(min_length=1, max_length=5)


class SlotProposalInternalOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    kind: str
    status: str
    slots: list[dict[str, str]]
    chosen_index: int | None
    task_id: uuid.UUID | None
    interview_id: uuid.UUID | None
    created_at: datetime


class CompliancePackIn(_In):
    period_month: date
    title: str = Field(min_length=2, max_length=200)


class CompliancePackDocumentIn(_In):
    document_id: uuid.UUID
    label: str = Field(min_length=1, max_length=200)


class CompliancePackItemInternalOut(BaseModel):
    id: uuid.UUID
    label: str
    file_id: uuid.UUID | None
    document_id: uuid.UUID | None


class DownloadLogOut(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID | None
    label: str | None
    client_user_id: uuid.UUID | None
    at: datetime


class CompliancePackInternalOut(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    period_month: date
    title: str
    published_at: datetime | None
    archived_at: datetime | None
    items: list[CompliancePackItemInternalOut]
    downloads: list[DownloadLogOut]
