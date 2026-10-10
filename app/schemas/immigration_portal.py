# ruff: noqa: E501
"""Worker "My Work" immigration DTOs (Epic 46 A3, AD-26). No owner remarks, counsel details, receipt or permit
numbers, outcome notes or internal ids beyond the Worker's own case / request ids. Also the public counsel /
no-portal Candidate upload page (purpose ``immigration_upload``): case reference (human id) and requested items
only."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.core.field_visibility import register_portal_schema


@register_portal_schema("worker")
class PortalRequestOut(BaseModel):
    id: uuid.UUID
    label: str
    for_dependant: str | None
    mandatory: bool
    status: str  # requested | uploaded | verified | rejected | waived
    rejection_reason: str | None
    due_date: date | None
    can_upload: bool


@register_portal_schema("worker")
class PortalCaseOut(BaseModel):
    id: uuid.UUID
    reference: str
    country: str
    visa_type_name: str
    status: str
    status_label: str
    stage_label: str | None
    open_requests: int
    updated_at: datetime


@register_portal_schema("worker")
class PortalCaseDetailOut(PortalCaseOut):
    requests: list[PortalRequestOut]


@register_portal_schema("worker")
class PortalNoticeOut(BaseModel):
    notice_id: uuid.UUID | None
    version: int | None
    title: str
    body_md: str


@register_portal_schema("worker")
class PortalCasesOut(BaseModel):
    cases: list[PortalCaseOut]
    consent_given: bool
    notice: PortalNoticeOut


class PublicUploadItemOut(BaseModel):
    id: uuid.UUID
    label: str
    status: str
    can_upload: bool


class PublicUploadViewOut(BaseModel):
    reference: str
    tenant_name: str | None
    expires_at: datetime
    uses_left: int
    items: list[PublicUploadItemOut]
    notice_title: str
    notice_body_md: str
    consent_required: bool


class PublicUploadOut(BaseModel):
    status: str
    item_id: uuid.UUID


# Epic 52 (phase-close gap, tests/portal/test_field_visibility.py): the public Secure Link upload page is opened by
# the Worker / Candidate (or their counsel) -- register its response models under the worker rules.
for _model in (PublicUploadItemOut, PublicUploadViewOut, PublicUploadOut):
    register_portal_schema("worker")(_model)
