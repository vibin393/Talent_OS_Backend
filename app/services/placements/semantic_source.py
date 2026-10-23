"""Semantic base source ``placements.worker`` (Epic 36 contract 0.6), registered from ``hooks.py``.

Root ``placements``; subject ``placements.candidate_id`` (the Worker); scope ``placement:read`` OR-ed over
``recruiter_user_id`` and ``account_manager_user_id``.
"""

from typing import Any

from sqlalchemy import func

from app.core.semantic import CMP, FieldDef, JoinDef, Op, ScopeSpec, register_semantic_source, sources
from app.models.candidates import Candidate
from app.models.placements import ENGAGEMENT_KINDS, PLACEMENT_STATUSES, WORKER_TYPES, Placement
from app.services.candidates.semantic_source import candidate_skill_predicate

__all__ = ["SOURCE_KEY", "register"]

SOURCE_KEY = "placements.worker"

_ID_OPS: frozenset[Op] = frozenset({"eq", "in"})
_DATE: frozenset[Op] = frozenset({"lt", "lte", "gt", "gte", "between", "within_days", "is_null"})  # CMP + between/within_days/is_null


def _uuid(label: str, attr: str) -> FieldDef:
    return FieldDef("uuid", label, "none", lambda p: getattr(p, attr), _ID_OPS, groupable=True)


def _enum(label: str, attr: str, values: tuple[str, ...]) -> FieldDef:
    return FieldDef(
        "enum", label, "none", lambda p: getattr(p, attr), _ID_OPS, enum_values=values, groupable=True
    )


def _date(label: str, expr: Any) -> FieldDef:
    return FieldDef("date", label, "none", expr, _DATE, groupable=True, date_buckets=True)


def _fields() -> dict[str, FieldDef]:
    return {
        "id": FieldDef("uuid", "Placement id", "none", lambda p: p.id, _ID_OPS),
        "human_id": FieldDef("string", "Placement number", "none", lambda p: p.human_id, _ID_OPS),
        "candidate_id": _uuid("Worker", "candidate_id"),
        "client_id": _uuid("Client", "client_id"),
        "legal_entity_id": _uuid("Legal entity", "legal_entity_id"),
        "account_manager_user_id": _uuid("Account manager", "account_manager_user_id"),
        "recruiter_user_id": _uuid("Recruiter", "recruiter_user_id"),
        "status": _enum("Status", "status", PLACEMENT_STATUSES),
        "worker_type": _enum("Worker type", "worker_type", WORKER_TYPES),
        "engagement_kind": _enum("Engagement", "engagement_kind", ENGAGEMENT_KINDS),
        "planned_start_date": _date("Planned start", lambda p: p.planned_start_date),
        "actual_start_date": _date("Actual start", lambda p: p.actual_start_date),
        "effective_end_date": _date("End date", lambda p: func.coalesce(p.end_date, p.planned_end_date)),
        "role_title": FieldDef(
            "string", "Role", "none", lambda p: p.role_title, frozenset({"contains", "eq"}), groupable=True
        ),
        "work_location_text": FieldDef(
            "string", "Work location", "none", lambda p: p.work_location_text, frozenset({"contains", "eq"})
        ),
        "compliance_blocked": FieldDef(
            "bool",
            "Compliance blocked",
            "none",
            lambda p: p.compliance_blocked,
            frozenset({"eq"}),
            groupable=True,
        ),
        "skills": FieldDef(
            "skill",
            "Worker skills",
            "none",
            lambda p: p.candidate_id,
            frozenset({"skill_match"}),
            skill_predicate=lambda p, expanded, min_p: candidate_skill_predicate(
                p.candidate_id, expanded, min_p
            ),
        ),
        "candidate.full_name": FieldDef(
            "string",
            "Worker name",
            "personal",
            lambda c: c.full_name,
            frozenset({"contains", "starts_with", "eq", "in"}),
            join="candidate",
        ),
        "candidate.city": FieldDef(
            "string",
            "Worker city",
            "personal",
            lambda c: c.location["city"].astext,
            frozenset({"eq", "in", "is_null"}),
            groupable=True,
            join="candidate",
        ),
        "candidate.notice_period_days": FieldDef(
            "int",
            "Worker notice period (days)",
            "none",
            lambda c: c.notice_period_days,
            CMP | {"between", "is_null"},
            join="candidate",
        ),
    }


def register() -> None:
    """Register ``placements.worker`` once per process (safe to call again)."""
    if SOURCE_KEY in sources():
        return
    register_semantic_source(
        SOURCE_KEY,
        Placement,
        _fields(),
        {"candidate": JoinDef(Candidate, lambda p, c: c.id == p.candidate_id)},
        ScopeSpec(
            permission="placement:read",
            owner_cols=(lambda p: p.recruiter_user_id, lambda p: p.account_manager_user_id),
        ),
        label="Workers (placements)",
        subject=lambda p: p.candidate_id,
    )
