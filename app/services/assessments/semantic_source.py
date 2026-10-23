# ruff: file-ignore[line-too-long]
"""Semantic source ``assessments.result`` (Epic 40 A11, FR-237 "Assessment results"), registered from ``hooks.py``.

Root ``assessment_invitations`` (erased, anonymous rows excluded); subject = the Candidate; scope ``assessment:read``
over the inviting user. Every field needs ``assessment:read``; the Candidate name (join) is ``personal``.
"""

from typing import Any

from sqlalchemy import select

from app.core.semantic import CMP, FieldDef, JoinDef, Op, ScopeSpec, register_semantic_source, sources
from app.models.assessments import (
    INVITATION_CONTEXT_KINDS,
    INVITATION_STATUSES,
    Assessment,
    AssessmentInvitation,
)
from app.models.candidates import Candidate

SOURCE_KEY = "assessments.result"
PERM = "assessment:read"
_ID_OPS: frozenset[Op] = frozenset({"eq", "in"})
_ENUM_OPS: frozenset[Op] = frozenset({"eq", "in", "not_in"})
_DATE: frozenset[Op] = CMP | frozenset({"between", "within_days", "is_null"})
_NUM: frozenset[Op] = CMP | frozenset({"between", "is_null"})


def _demand_of(application_id: Any) -> Any:
    from app.models.pipeline import Application

    return select(Application.demand_id).where(Application.id == application_id).scalar_subquery()


def _fields() -> dict[str, FieldDef]:
    return {
        "id": FieldDef("uuid", "Invitation id", "none", lambda i: i.id, _ID_OPS, permission=PERM),
        "assessment_id": FieldDef(
            "uuid", "Assessment", "none", lambda i: i.assessment_id, _ID_OPS, permission=PERM, groupable=True
        ),
        "candidate_id": FieldDef(
            "uuid", "Candidate", "none", lambda i: i.candidate_id, _ID_OPS, permission=PERM
        ),
        "status": FieldDef(
            "enum",
            "Status",
            "none",
            lambda i: i.status,
            _ENUM_OPS,
            permission=PERM,
            enum_values=INVITATION_STATUSES,
            groupable=True,
        ),
        "result": FieldDef(
            "enum",
            "Result",
            "none",
            lambda i: i.result,
            _ENUM_OPS | {"is_null"},
            permission=PERM,
            enum_values=("pass", "fail"),
            groupable=True,
        ),
        "score_pct": FieldDef(
            "decimal",
            "Score (%)",
            "none",
            lambda i: i.score,
            _NUM,
            permission=PERM,
            aggs=frozenset({"avg", "min", "max", "count"}),
        ),
        "percentile": FieldDef(
            "decimal",
            "Percentile",
            "none",
            lambda i: i.percentile,
            _NUM,
            permission=PERM,
            aggs=frozenset({"avg", "min", "max"}),
        ),
        "gating": FieldDef(
            "bool", "Gating", "none", lambda i: i.gating, frozenset({"eq"}), permission=PERM, groupable=True
        ),
        "context_kind": FieldDef(
            "enum",
            "Context",
            "none",
            lambda i: i.context_kind,
            _ENUM_OPS,
            permission=PERM,
            enum_values=INVITATION_CONTEXT_KINDS,
            groupable=True,
        ),
        "invited_at": FieldDef(
            "datetime",
            "Invited",
            "none",
            lambda i: i.created_at,
            _DATE,
            permission=PERM,
            groupable=True,
            date_buckets=True,
        ),
        "completed_at": FieldDef(
            "datetime",
            "Scored",
            "none",
            lambda i: i.scored_at,
            _DATE,
            permission=PERM,
            groupable=True,
            date_buckets=True,
        ),
        "deadline_at": FieldDef(
            "datetime", "Deadline", "none", lambda i: i.deadline_at, _DATE, permission=PERM
        ),
        "demand_id": FieldDef(
            "uuid",
            "Demand",
            "none",
            lambda i: _demand_of(i.application_id),
            _ID_OPS | {"is_null"},
            permission=PERM,
            groupable=True,
        ),
        "assessment.name": FieldDef(
            "string",
            "Assessment name",
            "none",
            lambda a: a.name,
            frozenset({"eq", "in", "contains"}),
            permission=PERM,
            groupable=True,
            join="assessment",
        ),
        "candidate.full_name": FieldDef(
            "string",
            "Candidate name",
            "personal",
            lambda c: c.full_name,
            frozenset({"contains", "starts_with", "eq"}),
            permission=PERM,
            join="candidate",
        ),
    }


def register() -> None:
    if SOURCE_KEY in sources():
        return
    register_semantic_source(
        SOURCE_KEY,
        AssessmentInvitation,
        _fields(),
        {
            "assessment": JoinDef(Assessment, lambda i, a: a.id == i.assessment_id),
            "candidate": JoinDef(Candidate, lambda i, c: c.id == i.candidate_id),
        },
        ScopeSpec(permission=PERM, owner_cols=(lambda i: i.invited_by_id,)),
        label="Assessment results",
        subject=lambda i: i.candidate_id,
        base_filter=lambda i: i.candidate_id.is_not(None),
    )


__all__ = ["SOURCE_KEY", "register"]
