"""Client Portal schemas of the interviews domain (Epic 18, FR-101): the Client User's pending scorecards (as
an external interviewer) and upcoming interview dates. No interviewer notes, other scorecards or kits beyond
what the P1 public scorecard page shows."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.field_visibility import register_portal_schema


@register_portal_schema("client_user")
class ClientScorecardItem(BaseModel):
    participant_id: uuid.UUID
    interview_id: uuid.UUID
    candidate_display_name: str
    demand_title: str | None
    stage_name: str | None
    starts_at: datetime
    state: str


@register_portal_schema("client_user")
class ClientUpcomingInterview(BaseModel):
    interview_id: uuid.UUID
    demand_title: str | None
    candidate_display_name: str
    starts_at: datetime
