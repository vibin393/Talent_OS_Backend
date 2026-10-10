"""Structured scoring inputs shared by the candidates, demands, ai and matching services (Epic 5, AD-8).

Explicit field whitelist: there is **no** name, date of birth / age, gender, religion, caste, marital status,
disability or photo field (PRD §8.1). ``tests/modules/matching/test_protected_attributes.py`` asserts the
field sets against an allowlist. Frozen dataclasses, so scoring can never mutate them.
"""

import dataclasses
import uuid
from datetime import date
from decimal import Decimal


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateSkillFact:
    skill_id: uuid.UUID
    name: str
    years: Decimal | None = None
    # Epic 37 (FR-190): ``candidate_skills.proficiency`` (1..5, None = not recorded).
    proficiency: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateMatchProfile:
    candidate_id: uuid.UUID
    current_title: str | None
    skills: tuple[CandidateSkillFact, ...]
    experience_years: Decimal | None
    city: str | None
    state: str | None
    country: str | None
    preferred_cities: tuple[str, ...]
    willing_to_relocate: bool | None
    work_auth: tuple[str, ...]
    # Expected compensation, annualised, in ``expected_comp_currency`` (None = not stated).
    expected_comp_annual: Decimal | None
    expected_comp_currency: str | None
    available_from: date | None
    notice_days: int | None
    domains: tuple[str, ...]
    role_titles: tuple[str, ...]
    employers: tuple[str, ...]
    certifications: tuple[str, ...]
    graduation_year: int | None = None  # a typical tenant-flagged proxy field (nulled when flagged)


@dataclasses.dataclass(frozen=True, slots=True)
class RequiredSkill:
    skill_id: uuid.UUID | None
    label: str
    mandatory: bool
    min_years: Decimal | None = None
    # Epic 37 (FR-190): minimum proficiency and per-skill graph expansion (``demand_skills``).
    min_proficiency: int | None = None
    expand: bool = True
    skill_ref_kind: str | None = None  # "global" | "tenant" (the SkillKey kind of ``skill_id``)


@dataclasses.dataclass(frozen=True, slots=True)
class SkillExpansion:
    """Skills that satisfy one required skill (Epic 37, ``skills_service.expand``; computed before scoring,
    AD-8). ``neighbours`` = ``(skill_id, weight, edge_label)`` with ``edge_label`` e.g. ``"implies Java"``."""

    required_skill_id: uuid.UUID
    neighbours: tuple[tuple[uuid.UUID, Decimal, str], ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class DemandMatchProfile:
    demand_id: uuid.UUID
    title: str
    skills: tuple[RequiredSkill, ...]
    experience_min: Decimal | None
    experience_max: Decimal | None
    remote: bool
    city: str | None
    state: str | None
    country: str | None
    required_work_auth: tuple[str, ...]
    # Budget (max pay rate), annualised, in ``currency``.
    budget_max_annual: Decimal | None
    currency: str | None
    needed_by: date | None
    domains: tuple[str, ...]
    summary: str | None = None
    # Epic 37 (FR-190): graph expansions of the required skills (empty = P1 behaviour).
    expansions: tuple[SkillExpansion, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class SkillGraph:
    """Skill hierarchy of the catalog: ``parents[child] = parent`` (global ``skills.parent_id`` and tenant
    ``tenant_skills.parent_skill_id``). Synonyms resolve to the same skill id when skills are stored."""

    parents: dict[uuid.UUID, uuid.UUID] = dataclasses.field(default_factory=dict)

    def parent_of(self, skill_id: uuid.UUID) -> uuid.UUID | None:
        return self.parents.get(skill_id)

    def children_of(self, skill_id: uuid.UUID) -> set[uuid.UUID]:
        return {child for child, parent in self.parents.items() if parent == skill_id}

    def descendants_of(self, skill_id: uuid.UUID, max_depth: int = 5) -> set[uuid.UUID]:
        out: set[uuid.UUID] = set()
        frontier = {skill_id}
        for _ in range(max_depth):
            frontier = {c for c, p in self.parents.items() if p in frontier} - out
            if not frontier:
                break
            out |= frontier
        return out


# Annualisation of compensation units (candidates: annual|monthly|daily|hourly; demands: year|month|day|hour).
ANNUAL_FACTOR: dict[str, Decimal] = {
    "annual": Decimal(1),
    "year": Decimal(1),
    "monthly": Decimal(12),
    "month": Decimal(12),
    "daily": Decimal(260),
    "day": Decimal(260),
    "hourly": Decimal(2080),
    "hour": Decimal(2080),
}


def annualise(amount: Decimal | None, unit: str | None) -> Decimal | None:
    if amount is None:
        return None
    return amount * ANNUAL_FACTOR.get((unit or "annual").lower(), Decimal(1))


# Fields of the candidate profile that may never exist (protected attributes, PRD §8.1).
PROTECTED_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "name",
        "full_name",
        "first_name",
        "last_name",
        "dob",
        "date_of_birth",
        "age",
        "gender",
        "sex",
        "religion",
        "caste",
        "marital_status",
        "disability",
        "photo",
        "photo_url",
        "nationality",
        "email",
        "phone",
    }
)

CANDIDATE_PROFILE_FIELDS: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(CandidateMatchProfile)
)
DEMAND_PROFILE_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(DemandMatchProfile))

# Fields a tenant may flag as proxies (``matching_settings.proxy_fields``); nulled before scoring/embedding.
PROXY_CAPABLE_FIELDS: frozenset[str] = frozenset(
    {
        "graduation_year",
        "employers",
        "city",
        "state",
        "country",
        "preferred_cities",
        "certifications",
        "domains",
    }
)


def null_proxies(
    profile: CandidateMatchProfile, proxy_fields: frozenset[str] | set[str]
) -> CandidateMatchProfile:
    """Return ``profile`` with every flagged proxy field emptied (None / empty tuple)."""
    changes: dict[str, object] = {}
    for field in proxy_fields:
        if field not in PROXY_CAPABLE_FIELDS or field not in CANDIDATE_PROFILE_FIELDS:
            continue
        current = getattr(profile, field)
        changes[field] = () if isinstance(current, tuple) else None
    return dataclasses.replace(profile, **changes) if changes else profile  # type: ignore[arg-type]
