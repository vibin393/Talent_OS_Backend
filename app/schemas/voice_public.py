# ruff: noqa: E501
"""Voice Screening consent page DTOs (Epic 58, Secure Link purpose ``voice_consent``). No ids or emails beyond what
the page needs."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints


class VoiceNoticeView(BaseModel):
    purpose: Literal["voice_screening", "call_recording"]
    title: str
    body: str


class VoiceConsentView(BaseModel):
    tenant_name: str | None
    recruiter_name: str | None
    candidate_first_name: str | None = None
    demand_title: str | None = None
    notices: list[VoiceNoticeView]
    country: str | None = None
    region: str | None = None
    voice_screening: Literal["granted", "refused", "pending"]
    call_recording: Literal["granted", "refused", "pending"]
    answered: bool
    expires_at: datetime | None = None


class VoiceConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]
    region: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None
    voice_screening: bool
    call_recording: bool = False


__all__ = ["VoiceConsentIn", "VoiceConsentView", "VoiceNoticeView"]
