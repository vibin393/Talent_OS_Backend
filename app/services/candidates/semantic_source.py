"""Semantic base source ``candidates.candidate`` (Epic 36 contract 0.6), registered from ``hooks.py``.

Root ``candidates`` (subject ``candidates.id``), base filter ``merged_into_id IS NULL AND erased_at IS NULL``,
scope ``candidate:read`` on ``owner_id``. Consent fields are display only (AD-42: consent is never a
membership filter). No field reads fairness data.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, Numeric, and_, cast, exists, false, func, literal_column, or_, select
from sqlalchemy.orm import aliased

from app.core.semantic import CMP, FieldDef, JoinDef, Op, ScopeSpec, register_semantic_source, sources
from app.core.skill_refs import ExpandedSkill
from app.models.candidates import CANDIDATE_SOURCES, Candidate, CandidateSkill, CandidateTag, Tag
from app.models.pipeline import Application, ApplicationStageHistory, PipelineStage
from app.models.placements import BENCH_STATUSES, PLACEMENT_STATUSES, WORKER_TYPES, BenchEntry, Placement
from app.models.privacy import Consent
from app.models.search import CandidateSearchDoc

__all__ = ["SOURCE_KEY", "candidate_skill_predicate", "register"]

SOURCE_KEY = "candidates.candidate"
CONSENT_VALUES = ("granted", "withdrawn", "none")

_STR: frozenset[Op] = frozenset({"contains", "starts_with", "eq", "in"})
_DATE: frozenset[Op] = frozenset({"lt", "lte", "gt", "gte", "between", "within_days", "is_null"})  # CMP + between/within_days/is_null


def candidate_skill_predicate(
    candidate_id: Any, expanded: Sequence[ExpandedSkill], min_proficiency: int | None
) -> ColumnElement[bool]:
    """Match a ``candidate_skills`` row of ``candidate_id`` for any expanded skill (``EXISTS``).

    A NULL proficiency fails a ``min_proficiency`` test.
    """
    global_ids = sorted({e.key.id for e in expanded if e.key.kind == "global"})
    tenant_ids = sorted({e.key.id for e in expanded if e.key.kind == "tenant"})
    cs = aliased(CandidateSkill)
    refs = []
    if global_ids:
        refs.append(cs.skill_id.in_(global_ids))
    if tenant_ids:
        refs.append(cs.tenant_skill_id.in_(tenant_ids))
    if not refs:
        return false()
    conds = [cs.candidate_id == candidate_id, or_(*refs)]
    if min_proficiency is not None:
        conds.append(cs.proficiency >= min_proficiency)
    return exists(select(literal_column("1")).select_from(cs).where(and_(*conds)))


def _consent(purpose: str) -> Any:
    def expr(c: Any) -> Any:
        k = aliased(Consent)
        latest = (
            select(k.status)
            .where(k.candidate_id == c.id, k.purpose == purpose, k.client_id.is_(None))
            .order_by(k.updated_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        return func.coalesce(latest, literal_column("'none'"))

    return expr


def _stage_history() -> Any:
    h, a, s = aliased(ApplicationStageHistory), aliased(Application), aliased(PipelineStage)
    return (
        select(a.candidate_id.label("candidate_id"), s.key.label("stage_key"), h.at.label("entered_at"))
        .select_from(h)
        .join(a, a.id == h.application_id)
        .join(s, s.id == h.to_stage_id)
        .subquery("stage_history")
    )


def _tags() -> Any:
    ct, t = aliased(CandidateTag), aliased(Tag)
    return (
        select(ct.candidate_id.label("candidate_id"), t.name.label("name"))
        .select_from(ct)
        .join(t, t.id == ct.tag_id)
        .subquery("candidate_tag_names")
    )


def _fields() -> dict[str, FieldDef]:
    f = FieldDef
    return {
        "id": f("uuid", "Candidate id", "none", lambda c: c.id, frozenset({"eq", "in", "not_in"})),
        "human_id": f(
            "string", "Candidate number", "none", lambda c: c.human_id, frozenset({"eq", "in", "starts_with"})
        ),
        "full_name": f("string", "Name", "personal", lambda c: c.full_name, _STR),
        "current_title": f(
            "string", "Current title", "personal", lambda c: c.current_title, _STR, groupable=True
        ),
        "current_employer": f(
            "string", "Current employer", "personal", lambda c: c.current_employer, _STR, groupable=True
        ),
        "skills": f(
            "skill",
            "Skills",
            "none",
            lambda c: c.id,
            frozenset({"skill_match"}),
            skill_predicate=lambda c, expanded, min_p: candidate_skill_predicate(c.id, expanded, min_p),
        ),
        "experience_years": f(
            "decimal",
            "Experience (years)",
            "none",
            lambda c: cast(c.total_experience_months, Numeric) / 12,
            CMP | {"between", "is_null"},
            aggs=frozenset({"avg", "min", "max", "sum"}),
        ),
        "city": f(
            "string",
            "City",
            "personal",
            lambda c: c.location["city"].astext,
            frozenset({"eq", "in", "is_null"}),
            groupable=True,
        ),
        "state": f(
            "string",
            "State",
            "none",
            lambda c: c.location["state"].astext,
            frozenset({"eq", "in", "is_null"}),
            groupable=True,
        ),
        "country": f(
            "string",
            "Country",
            "none",
            lambda c: c.location["country"].astext,
            frozenset({"eq", "in", "is_null"}),
            groupable=True,
        ),
        "notice_period_days": f(
            "int",
            "Notice period (days)",
            "none",
            lambda c: c.notice_period_days,
            CMP | {"between", "is_null"},
            aggs=frozenset({"avg", "min", "max"}),
        ),
        "availability_date": f(
            "date",
            "Available from",
            "none",
            lambda c: c.availability_date,
            _DATE,
            groupable=True,
            date_buckets=True,
        ),
        "expected_comp_inr": f(
            "decimal",
            "Expected compensation (INR)",
            "compensation",
            lambda d: d.expected_comp_inr,
            CMP | {"between", "is_null"},
            aggs=frozenset({"avg", "min", "max"}),
            join="search_doc",
        ),
        "work_authorization": f(
            "string",
            "Work authorization",
            "personal",
            lambda c: c.work_authorization,
            frozenset({"eq", "in", "contains", "is_null"}),
        ),
        "willing_to_relocate": f(
            "bool",
            "Willing to relocate",
            "none",
            lambda c: c.willing_to_relocate,
            frozenset({"eq", "is_null"}),
            groupable=True,
        ),
        "tags.name": f("string", "Tag", "none", lambda t: t.c.name, frozenset({"eq", "in"}), join="tags"),
        "source": f(
            "enum",
            "Source",
            "none",
            lambda c: c.source,
            frozenset({"eq", "in", "not_in"}),
            enum_values=CANDIDATE_SOURCES,
            groupable=True,
            aggs=frozenset({"count"}),
        ),
        "owner_id": f("uuid", "Owner", "none", lambda c: c.owner_id, frozenset({"eq", "in"}), groupable=True),
        "created_at": f(
            "datetime",
            "Created",
            "none",
            lambda c: c.created_at,
            CMP | {"between", "within_days"},
            groupable=True,
            date_buckets=True,
        ),
        "last_updated_at": f(
            "datetime",
            "Last activity",
            "none",
            lambda c: c.updated_at,
            CMP | {"between", "within_days"},
            groupable=True,
            date_buckets=True,
        ),
        "is_archived": f(
            "bool",
            "Archived",
            "none",
            lambda c: c.archived_at.is_not(None),
            frozenset({"eq"}),
            groupable=True,
        ),
        "stage_history.stage_key": f(
            "string",
            "Reached stage",
            "none",
            lambda h: h.c.stage_key,
            frozenset({"eq", "in"}),
            join="stage_history",
        ),
        "stage_history.entered_at": f(
            "datetime",
            "Reached stage at",
            "none",
            lambda h: h.c.entered_at,
            CMP | {"within_days"},
            join="stage_history",
        ),
        "placements.status": f(
            "enum",
            "Placement status",
            "none",
            lambda p: p.status,
            frozenset({"eq", "in"}),
            enum_values=PLACEMENT_STATUSES,
            join="placements",
        ),
        "placements.worker_type": f(
            "enum",
            "Worker type",
            "none",
            lambda p: p.worker_type,
            frozenset({"eq", "in"}),
            enum_values=WORKER_TYPES,
            join="placements",
        ),
        "placements.end_date": f(
            "date",
            "Placement end date",
            "none",
            lambda p: func.coalesce(p.end_date, p.planned_end_date),
            CMP | {"within_days", "is_null"},
            join="placements",
        ),
        "bench.status": f(
            "enum",
            "Bench status",
            "none",
            lambda b: b.status,
            frozenset({"eq", "in"}),
            enum_values=BENCH_STATUSES,
            join="bench",
        ),
        "consent_marketing": f(
            "enum",
            "Marketing consent",
            "personal",
            _consent("marketing"),
            frozenset(),
            enum_values=CONSENT_VALUES,
        ),
        "consent_engagement": f(
            "enum",
            "Engagement consent",
            "personal",
            _consent("engagement"),
            frozenset(),
            enum_values=CONSENT_VALUES,
        ),
    }


def _joins() -> dict[str, JoinDef]:
    return {
        "search_doc": JoinDef(CandidateSearchDoc, lambda c, d: d.candidate_id == c.id),
        "tags": JoinDef(_tags(), lambda c, t: t.c.candidate_id == c.id, many=True),
        "stage_history": JoinDef(_stage_history(), lambda c, h: h.c.candidate_id == c.id, many=True),
        "placements": JoinDef(
            Placement, lambda c, p: p.candidate_id == c.id, many=True, permission="placement:read"
        ),
        "bench": JoinDef(BenchEntry, lambda c, b: b.candidate_id == c.id, many=True, permission="bench:read"),
    }


def register() -> None:
    """Register ``candidates.candidate`` once per process (safe to call again)."""
    if SOURCE_KEY in sources():
        return
    register_semantic_source(
        SOURCE_KEY,
        Candidate,
        _fields(),
        _joins(),
        ScopeSpec(permission="candidate:read", owner_cols=(lambda c: c.owner_id,)),
        label="Candidates",
        subject=lambda c: c.id,
        base_filter=lambda c: and_(c.merged_into_id.is_(None), c.erased_at.is_(None)),
    )
