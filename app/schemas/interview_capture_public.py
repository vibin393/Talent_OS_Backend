"""Interview Assistant public / portal DTOs (Epic 60): the Candidate's recording consent page and the external
interviewer's acknowledgement page. No ids or emails beyond what the page needs."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordingConsentView(BaseModel):
    tenant_name: str | None
    candidate_first_name: str | None = None
    stage_name: str | None = None
    starts_at: datetime | None = None
    status: Literal["agreed", "refused", "waiting"]
    notice_title: str
    notice_body: str
    country: str | None = None
    region: str | None = None


class RecordingConsentIn(_In):
    decision: Literal["agree", "refuse"]
    country: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)] | None = (
        None
    )
    region: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None


class CaptureAckView(BaseModel):
    tenant_name: str | None
    participant_name: str | None
    stage_name: str | None = None
    starts_at: datetime | None = None
    statement: str
    status: Literal["agreed", "waiting"]


class CaptureAckIn(_In):
    agree: Literal[True]


__all__ = ["CaptureAckIn", "CaptureAckView", "RecordingConsentIn", "RecordingConsentView"]
