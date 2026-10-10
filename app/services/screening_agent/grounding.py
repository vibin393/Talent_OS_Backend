# ruff: noqa: E501
"""Grounded Answers (Epic 57 A3, FR-274 / coordinator decision 3).

* ``posting_fields`` -- the published posting fields of the conversation's Demand (only when the posting is live).
  Pay and rates are never included: compensation questions are handed off.
* ``grounded_checker`` -- registered as ``screening``: a conversation the candidate accepted (``consented`` /
  ``in_progress``) is a valid target for grounded sends. It returns False for ids that are not screening
  conversations, so the other owners' checkers decide theirs.
* ``check_reply_text`` -- the commitment validator: no promises, outcomes or amounts.
"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.demands import Demand, DemandPosting
from app.models.screening_agent import ScreeningConversation

FIELD_LABELS: dict[str, str] = {
    "title": "Job title",
    "employment_type": "Employment type",
    "work_mode": "Work mode",
    "location": "Location",
    "shift": "Shift",
    "travel_percent": "Travel (%)",
    "start_date": "Start date",
    "min_experience_years": "Minimum experience (years)",
    "max_experience_years": "Maximum experience (years)",
    "education": "Education",
    "languages": "Languages",
    "department": "Department",
    "positions": "Open positions",
}
COMMITMENT_RE = re.compile(
    r"\b(we will|we'll|you will (get|be|receive)|you'll (get|be|receive)|guarantee[ds]?|promise[ds]?|definitely|"
    r"offer you|you are (selected|shortlisted|hired)|interview (is|will be) (scheduled|confirmed)|"
    r"(rs\.?|inr|₹|\$|usd|lpa|lakh|lakhs|crore|ctc|salary of)\s*\d|\d+\s*(lpa|lakh|lakhs|crore|k per month))",
    re.IGNORECASE,
)


async def posting_fields(db: AsyncSession, demand_id: uuid.UUID) -> list[dict[str, Any]]:
    posting = (
        await db.execute(
            select(DemandPosting).where(DemandPosting.demand_id == demand_id, DemandPosting.is_live.is_(True))
        )
    ).scalar_one_or_none()
    if posting is None:
        return []
    demand = await db.get(Demand, demand_id)
    if demand is None:
        return []
    out: list[dict[str, Any]] = []
    for key, label in FIELD_LABELS.items():
        if key == "location" and not posting.show_location:
            continue
        value = getattr(demand, key, None)
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        out.append({"key": key, "label": label, "value": str(value)[:300]})
    return out


def check_reply_text(text: str) -> str | None:
    """None when the text makes no commitment; else the offending fragment."""
    found = COMMITMENT_RE.search(text)
    return found.group(0) if found else None


async def grounded_checker(ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID) -> bool:  # noqa: ARG001
    row = (
        await db.execute(
            select(ScreeningConversation.status).where(ScreeningConversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    return row in ("consented", "in_progress")


__all__ = ["FIELD_LABELS", "check_reply_text", "grounded_checker", "posting_fields"]
