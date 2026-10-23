# ruff: file-ignore[line-too-long]
"""Recording Policy gate (FR-294, coordinator decision 9; Epic 53 spec 0.13). Epic 58 owns the settings UI / API.

Rules:

1. The jurisdiction is ``confirmed_jurisdiction`` (from the consent page) else the Candidate's profile location
   (country / state). An unknown location is ``not_permitted`` (``unknown_jurisdiction``).
2. The rule is the region row, else the country row, else ``not_permitted``.
3. ``candidate_consent`` needs a granted ``purpose`` consent (``missing_consent:<purpose>``).
4. ``all_party_consent`` also needs an ack from every participant User (``missing_ack:<user_id>``).

``seed_tenant_defaults`` inserts ``IN`` / NULL / ``candidate_consent`` for the session tenant (idempotent; called
from tenant provisioning, migration ``20261008_400_03`` seeds existing tenants).
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.recording_policy import RecordingPolicy
from app.services import entity_refs
from app.services.privacy.consent import has_active_consent

Rule = Literal["not_permitted", "candidate_consent", "all_party_consent", "unknown"]
Purpose = Literal["call_recording", "interview_recording"]

SEED_NOTE = "Seeded default (coordinator decision 9): India, recording with the candidate's consent."

# Free-text profile countries -> ISO 3166-1 alpha-2 (two-letter codes pass through).
_COUNTRY_ALIASES: dict[str, str] = {
    "india": "IN", "bharat": "IN", "united states": "US", "united states of america": "US", "usa": "US",
    "us": "US", "america": "US", "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "canada": "CA", "australia": "AU", "singapore": "SG", "united arab emirates": "AE", "uae": "AE",
    "germany": "DE", "france": "FR", "netherlands": "NL", "ireland": "IE", "new zealand": "NZ",
    "japan": "JP", "philippines": "PH", "sri lanka": "LK", "nepal": "NP", "bangladesh": "BD",
    "saudi arabia": "SA", "qatar": "QA", "malaysia": "MY", "south africa": "ZA",
}  # fmt: skip

_candidates = table("candidates", column("id"), column("location"))


@dataclasses.dataclass(frozen=True)
class GateDecision:
    recording_allowed: bool
    rule: Rule
    jurisdiction: str | None
    reasons: tuple[str, ...]
    required: tuple[str, ...]


def normalize_country(value: str | None) -> str | None:
    key = " ".join((value or "").replace(".", "").split()).lower()
    if not key:
        return None
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    if len(key) == 2 and key.isalpha():
        return key.upper()
    return None


def normalize_region(value: str | None) -> str | None:
    key = " ".join((value or "").split()).upper()
    return key[:10] or None


def _label(country: str, region: str | None) -> str:
    return f"{country}-{region}" if region else country


async def _profile_location(db: AsyncSession, candidate_id: uuid.UUID) -> tuple[str | None, str | None]:
    loc: Any = (
        await db.execute(select(_candidates.c.location).where(_candidates.c.id == candidate_id))
    ).scalar_one_or_none()
    if not isinstance(loc, Mapping):
        return None, None
    return normalize_country(loc.get("country")), normalize_region(loc.get("state"))


async def _rule_for(db: AsyncSession, country: str, region: str | None) -> str:
    rows = (
        (await db.execute(select(RecordingPolicy).where(RecordingPolicy.country == country))).scalars().all()
    )
    if region is not None:
        for row in rows:
            if row.region is not None and row.region.upper() == region:
                return row.rule
    for row in rows:
        if row.region is None:
            return row.rule
    return "not_permitted"


async def check(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    purpose: Purpose,
    candidate_id: uuid.UUID,
    participant_user_ids: Sequence[uuid.UUID] = (),
    acks: Mapping[uuid.UUID, datetime] = {},
    confirmed_jurisdiction: tuple[str, str | None] | None = None,
) -> GateDecision:
    """May ``purpose`` record this Candidate now? Never raises for a policy outcome; 404 if not readable."""
    await entity_refs.require_readable(ctx, db, "candidate", candidate_id)
    if confirmed_jurisdiction is not None:
        country, region = (
            normalize_country(confirmed_jurisdiction[0]),
            normalize_region(confirmed_jurisdiction[1]),
        )
    else:
        country, region = await _profile_location(db, candidate_id)
    if country is None:
        return GateDecision(False, "unknown", None, ("unknown_jurisdiction", "not_permitted"), ())
    jurisdiction = _label(country, region)
    rule = await _rule_for(db, country, region)
    if rule == "not_permitted":
        return GateDecision(False, "not_permitted", jurisdiction, ("not_permitted",), ())
    required: list[str] = [f"consent:{purpose}"]
    reasons: list[str] = []
    if not await has_active_consent(db, candidate_id, purpose):
        reasons.append(f"missing_consent:{purpose}")
    if rule == "all_party_consent":
        for uid in dict.fromkeys(participant_user_ids):
            required.append(f"ack:{uid}")
            if acks.get(uid) is None:
                reasons.append(f"missing_ack:{uid}")
    return GateDecision(not reasons, rule, jurisdiction, tuple(reasons), tuple(required))  # type: ignore[arg-type]


async def seed_tenant_defaults(db: AsyncSession) -> None:
    """Insert ``IN`` / NULL / ``candidate_consent`` for the session's tenant unless an ``IN`` country row exists."""
    tenant_id = entity_refs.session_tenant(db)
    exists = (
        await db.execute(
            select(RecordingPolicy.id).where(
                RecordingPolicy.country == "IN", RecordingPolicy.region.is_(None)
            )
        )
    ).first()
    if exists is not None:
        return
    db.add(
        RecordingPolicy(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            country="IN",
            region=None,
            rule="candidate_consent",
            note=SEED_NOTE,
        )
    )
    await db.flush()


__all__ = ["GateDecision", "check", "normalize_country", "normalize_region", "seed_tenant_defaults"]
