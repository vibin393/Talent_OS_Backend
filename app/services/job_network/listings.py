# ruff: file-ignore[line-too-long]
"""Tenant-side eligibility and projection (Epic 75, spec A2 / A3).

The projection copies **only** the careers-public fields of a Demand Posting into the shape of ``network_listings``
(allow-list ``constants.PROJECTED_FIELDS``). It reuses the careers public projection -- the network shows exactly what
the careers site already shows to the world -- and never reads recruiter/owner names, emails, phones, internal notes,
apply questions, demand ids, client ids, rates beyond the shown range, headcount, margins, BU, team or approvals.
"""

import dataclasses
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.demands import Demand, DemandPosting
from app.services.job_network.constants import (
    DEFAULT_VALID_THROUGH_DAYS,
    ELIGIBILITY_REASONS,
    PROJECTED_FIELDS,
)
from app.services.portal import careers, projection
from app.services.portal.common import plain_text


@dataclasses.dataclass(frozen=True, slots=True)
class EligibilityInput:
    is_live: bool  # careers ``_live`` rule holds
    visibility: str
    client_id: uuid.UUID
    exclude_client_ids: tuple[uuid.UUID, ...]
    joined: bool
    member_status: str | None
    verification_status: str | None
    sandbox: bool
    list_mode: str
    include_choice: bool | None  # explicit per-demand choice (None = none)
    live_count: int
    listing_limit: int


def eligible(data: EligibilityInput) -> tuple[bool, list[str]]:
    """Pure eligibility decision. Returns ``(eligible, reasons)``; when eligible, ``reasons`` is empty."""
    reasons: list[str] = []
    if data.visibility == "internal":
        reasons.append("internal_visibility")
    if not data.is_live:
        reasons.append("not_live")
    if data.client_id in data.exclude_client_ids:
        reasons.append("client_excluded")
    if data.sandbox:
        reasons.append("sandbox")
    if not data.joined:
        reasons.append("not_joined")
    elif data.member_status == "suspended":
        reasons.append("member_suspended")
    elif data.member_status != "active" or data.verification_status != "verified":
        reasons.append("member_pending")
    # opt-in resolution: an explicit ``include=false`` always wins; ``choose`` mode needs an explicit ``include=true``.
    if data.include_choice is False or (data.list_mode == "choose" and data.include_choice is not True):
        reasons.append("opted_out")
    if data.include_choice is not True and data.live_count >= data.listing_limit:
        reasons.append("limit_reached")
    assert all(r in ELIGIBILITY_REASONS for r in reasons)
    return (not reasons, reasons)


@dataclasses.dataclass(frozen=True, slots=True)
class NetworkListingData:
    tenant_id: uuid.UUID
    posting_ref: uuid.UUID
    title: str
    description_html: str
    description_text: str
    client_name: str | None
    city: str | None
    region: str | None
    country: str | None
    location_hidden: bool
    work_mode: str | None
    employment_type: str | None
    experience_min: Decimal | None
    experience_max: Decimal | None
    skills: list[str]
    category: str | None
    pay_min: Decimal | None
    pay_max: Decimal | None
    pay_currency: str | None
    pay_period: str | None
    apply_url: str
    posting_url: str
    published_at: datetime
    valid_through: datetime | None
    content_sha256: str

    def public_fields(self) -> dict[str, Any]:
        """Every projected field (keys are a subset of ``PROJECTED_FIELDS``; routing keys excluded)."""
        d = {k: getattr(self, k) for k in PROJECTED_FIELDS}
        return d


def _country(value: Any) -> str | None:
    s = str(value or "").strip().upper()
    return s[:2] if len(s) == 2 else None


def _content_hash(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, default=str, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def load_eligibility_input(
    db: AsyncSession,
    *,
    demand_id: uuid.UUID,
    exclude_client_ids: tuple[uuid.UUID, ...],
    joined: bool,
    member_status: str | None,
    verification_status: str | None,
    sandbox: bool,
    list_mode: str,
    include_choice: bool | None,
    live_count: int,
    listing_limit: int,
) -> tuple[EligibilityInput, DemandPosting | None]:
    """Build the eligibility input for a demand (tenant session). The posting may not exist / not be live."""
    row = (
        await db.execute(
            select(DemandPosting, Demand)
            .join(Demand, Demand.id == DemandPosting.demand_id)
            .where(DemandPosting.demand_id == demand_id)
            .order_by(DemandPosting.published_at.desc().nulls_last())
            .limit(1)
        )
    ).first()
    if row is None:
        demand = (await db.execute(select(Demand).where(Demand.id == demand_id))).scalar_one_or_none()
        if demand is None:
            raise careers.posting_not_found()
        data = EligibilityInput(
            is_live=False, visibility=demand.visibility, client_id=demand.client_id,
            exclude_client_ids=exclude_client_ids, joined=joined, member_status=member_status,
            verification_status=verification_status, sandbox=sandbox, list_mode=list_mode,
            include_choice=include_choice, live_count=live_count, listing_limit=listing_limit,
        )
        return data, None
    posting, demand = row
    # ``careers._live`` is expressed as SQL; re-check it in Python for this already-loaded row.
    from app.services.portal.careers import LIVE_DEMAND_STATUSES

    is_live = bool(
        posting.is_live
        and demand.status in LIVE_DEMAND_STATUSES
        and demand.archived_at is None
        and demand.visibility != "internal"
    )
    data = EligibilityInput(
        is_live=is_live, visibility=demand.visibility, client_id=demand.client_id,
        exclude_client_ids=exclude_client_ids, joined=joined, member_status=member_status,
        verification_status=verification_status, sandbox=sandbox, list_mode=list_mode,
        include_choice=include_choice, live_count=live_count, listing_limit=listing_limit,
    )
    return data, posting


async def project(
    db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID, *, careers_base_url: str
) -> NetworkListingData | None:
    """Project the demand's live posting into ``NetworkListingData`` (tenant session). Returns ``None`` when the posting
    is not live (so the caller unlists)."""
    row = (
        await db.execute(careers._base().where(DemandPosting.demand_id == demand_id))
    ).first()
    if row is None:
        return None
    posting, demand, client_name = row
    base = careers._row(posting, demand, client_name)
    detail = await careers._detail_extras(db, base)
    # Reuse the careers public projection so every field equals a careers public field.
    pay = projection._pay_range(detail)
    description_html = projection.render_markdown(detail.get("content") or "")
    description_text = plain_text(description_html)
    show_location = bool(detail["show_location"])
    location = detail.get("location") if show_location else None
    country = _country(detail.get("country")) if show_location else None
    fields: dict[str, Any] = {
        "title": detail["title"],
        "description_html": description_html,
        "description_text": description_text,
        "client_name": projection._client(detail),
        "city": location,
        "region": None,
        "country": country,
        "location_hidden": not show_location,
        "work_mode": detail.get("work_mode"),
        "employment_type": detail.get("employment_type"),
        "experience_min": _dec(detail.get("min_experience_years")),
        "experience_max": _dec(detail.get("max_experience_years")),
        "skills": [str(s) for s in (detail.get("skills") or [])][:30],
        "category": None,
        "pay_min": _dec(pay.min) if pay else None,
        "pay_max": _dec(pay.max) if pay else None,
        "pay_currency": (pay.currency[:3].upper() if pay and pay.currency else None),
        "pay_period": (_period(pay.unit) if pay else None),
        "apply_url": f"{careers_base_url.rstrip('/')}/jobs/{posting.id}/apply",  # the ``src``/``ref`` are appended at index time
        "posting_url": f"{careers_base_url.rstrip('/')}/jobs/{posting.id}",
        "published_at": posting.published_at or detail.get("published_at"),
        "valid_through": None,
    }
    published = fields["published_at"]
    if isinstance(published, datetime):
        fields["valid_through"] = published + timedelta(days=DEFAULT_VALID_THROUGH_DAYS)
    fields["content_sha256"] = _content_hash({k: v for k, v in fields.items() if k != "content_sha256"})
    assert set(fields) <= PROJECTED_FIELDS, set(fields) - PROJECTED_FIELDS
    return NetworkListingData(tenant_id=tenant_id, posting_ref=posting.id, **fields)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _period(unit: str | None) -> str | None:
    return {"hour": "hour", "day": "day", "month": "month", "year": "year"}.get((unit or "").lower())


__all__ = ["EligibilityInput", "NetworkListingData", "eligible", "load_eligibility_input", "project"]
