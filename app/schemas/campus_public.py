# ruff: file-ignore[line-too-long]
"""Public campus pages (Epic 43, AD-53): ``/api/v1/public/campus/r/{token}`` (drive registration, Secure Link purpose
``campus_registration``) and ``/api/v1/public/campus/e/{token}`` (recruitment event registration or a personal
check-in pass, purpose ``event_checkin``). Never returns other registrants' data."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicNotice(BaseModel):
    notice_id: uuid.UUID | None
    version: int | None
    title: str
    body_md: str


class PublicFormField(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    options: list[str] | None = None
    min: str | None = None
    max: str | None = None
    help: str | None = None


class PublicDriveView(BaseModel):
    status: Literal["open", "closed"]
    tenant_name: str | None
    drive_name: str
    institution_name: str
    batch_year: int
    closes_at: datetime | None
    fields: list[PublicFormField]
    branches_by_program: dict[str, list[str]]
    notice: PublicNotice
    marketing_notice: PublicNotice | None
    prefill: dict[str, Any] = {}
    challenge: dict[str, str] | None = None


class PublicRegisterIn(_In):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    answers: dict[str, Any] = Field(default_factory=dict)
    notice_id: uuid.UUID | None = None
    agreed: bool = False
    marketing_opt_in: bool = False
    bot_token: str | None = Field(default=None, max_length=4000)


class PublicRegisterOut(BaseModel):
    status: Literal["registered", "already_registered"]


class PublicEventDemand(BaseModel):
    id: uuid.UUID
    title: str


class PublicEventView(BaseModel):
    mode: Literal["register", "pass"]
    status: Literal["open", "closed"]
    tenant_name: str | None
    event_name: str
    kind: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    venue: str | None
    video_link: str | None
    demands: list[PublicEventDemand]
    notice: PublicNotice | None
    marketing_notice: PublicNotice | None
    whatsapp_opt_in: bool
    candidate_first_name: str | None = None
    checkin_url: str | None = None
    qr_svg: str | None = None
    challenge: dict[str, str] | None = None


class PublicEventRegisterIn(_In):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    demand_id: uuid.UUID | None = None
    notice_id: uuid.UUID | None = None
    agreed: bool = False
    marketing_opt_in: bool = False
    whatsapp_opt_in: bool = False
    bot_token: str | None = Field(default=None, max_length=4000)


class PublicEventRegisterOut(BaseModel):
    status: Literal["registered", "already_registered"]
    checkin_url: str | None = None
    qr_svg: str | None = None
