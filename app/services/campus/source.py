# ruff: noqa: E501
"""Semantic source ``campus.registration`` (owned by Epic 43, AD-42) and the campus fields added to the base
``candidates.candidate`` source (foundation 0.6 deviation note: "Campus batch is added by 43").

``campus.registration``: root ``registrations``, subject ``registrations.id``; scope ``campus:read`` on the drive
owner (correlated scalar subquery). Fields: ``id``, ``drive_id``, ``drive_institution.institution_id`` and
``institution.tier`` (one-joins), ``program``, ``branch``, ``cgpa``, ``percentage``, ``backlogs``,
``graduation_year``, ``batch_year``, ``source``, ``status``, ``registered_at``. Academic fields are ``personal``; no
candidate identity, protected or free-text answer is exposed. Eligibility evaluates it with a system context.

``candidates.candidate`` gains ``campus_batch`` (string array of the Candidate's registration batch years -- the field
Epic 38's ``campus_<batch>`` Pool template requires) and the many-join ``campus_registrations`` with
``campus_registrations.batch_year`` / ``.drive_id`` / ``.status`` (filters only).
"""

from typing import Any

from sqlalchemy import String, cast, func, literal_column, select, type_coerce
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import aliased

from app.core.semantic import (
    CMP,
    FieldDef,
    JoinDef,
    Op,
    ScopeSpec,
    extend_semantic_source,
    get_source,
    register_semantic_source,
    sources,
)
from app.models.campus import (
    INSTITUTION_TIERS,
    REGISTRATION_SOURCES,
    REGISTRATION_STATUSES,
    CampusDrive,
    DriveInstitution,
    Institution,
    Registration,
)
from app.services.campus.common import SEMANTIC_SOURCE

_ID: frozenset[Op] = frozenset({"eq", "in", "not_in"})
_ENUM: frozenset[Op] = frozenset({"eq", "ne", "in", "not_in"})
_STR: frozenset[Op] = frozenset({"eq", "ne", "in", "not_in", "is_null"})
_NUM: frozenset[Op] = CMP | frozenset({"eq", "ne", "in", "not_in", "between", "is_null"})
_DT: frozenset[Op] = CMP | frozenset({"between", "within_days"})
_queued: set[str] = set()


def _drive_owner(r: Any) -> Any:
    return select(CampusDrive.owner_id).where(CampusDrive.id == r.drive_id).scalar_subquery()


def _num(label: str, attr: str, t: str = "decimal") -> FieldDef:
    return FieldDef(t, label, "personal", lambda r: getattr(r, attr), _NUM, aggs=frozenset({"avg", "min", "max"}))  # type: ignore[arg-type]


def registration_fields() -> dict[str, FieldDef]:
    return {
        "id": FieldDef("uuid", "Registration", "none", lambda r: r.id, _ID),
        "drive_id": FieldDef("uuid", "Campus drive", "none", lambda r: r.drive_id, frozenset({"eq", "in"}), groupable=True),
        "drive_institution.institution_id": FieldDef(
            "uuid", "Institution", "none", lambda t: t.institution_id, _ID, groupable=True, join="drive_institution"
        ),
        "institution.tier": FieldDef(
            "enum", "Institution tier", "none", lambda t: t.tier, _ENUM, enum_values=INSTITUTION_TIERS, groupable=True, join="institution"
        ),
        "program": FieldDef("string", "Program", "personal", lambda r: r.program, _STR, groupable=True),
        "branch": FieldDef("string", "Branch", "personal", lambda r: r.branch, _STR, groupable=True),
        "cgpa": _num("CGPA", "cgpa"),
        "percentage": _num("Percentage", "percentage"),
        "backlogs": _num("Active backlogs", "backlogs", "int"),
        "graduation_year": FieldDef("int", "Graduation year", "personal", lambda r: r.graduation_year, _NUM, groupable=True),
        "batch_year": FieldDef("int", "Batch", "none", lambda r: r.batch_year, _NUM, groupable=True),
        "source": FieldDef("enum", "Registered via", "none", lambda r: r.source, _ENUM, enum_values=REGISTRATION_SOURCES, groupable=True),
        "status": FieldDef("enum", "Status", "none", lambda r: r.status, _ENUM, enum_values=REGISTRATION_STATUSES, groupable=True),
        "registered_at": FieldDef("datetime", "Registered on", "none", lambda r: r.registered_at, _DT, groupable=True, date_buckets=True),
    }


def _campus_batch(c: Any) -> Any:
    r = aliased(Registration)
    sub = select(func.array_agg(cast(r.batch_year, String))).where(r.candidate_id == c.id, r.status != "withdrawn")
    # coalesce(...) makes it an array expression: ``x = ANY(<array>)`` instead of the set form ``ANY (SELECT ...)``.
    return type_coerce(func.coalesce(sub.scalar_subquery(), literal_column("'{}'::varchar[]")), ARRAY(String))


def _missing(source: str, key: str) -> bool:
    if source in sources():
        return key not in get_source(source).fields
    if source in _queued:
        return False
    _queued.add(source)
    return True


def register() -> None:
    """Idempotent: registers ``campus.registration`` and extends ``candidates.candidate`` once per process (re-applied
    when a test re-registers the base source)."""
    if SEMANTIC_SOURCE not in sources():
        register_semantic_source(
            SEMANTIC_SOURCE,
            Registration,
            registration_fields(),
            {
                "drive_institution": JoinDef(DriveInstitution, lambda r, t: t.id == r.drive_institution_id),
                "institution": JoinDef(Institution, lambda r, t: t.id == r.institution_id),
            },
            ScopeSpec(permission="campus:read", owner_cols=(_drive_owner,)),
            label="Campus registrations",
            subject=lambda r: r.id,
        )
    if _missing("candidates.candidate", "campus_batch"):
        extend_semantic_source(
            "candidates.candidate",
            fields={
                "campus_batch": FieldDef(
                    "string_array", "Campus batch", "none", _campus_batch, frozenset({"eq", "in", "is_null"})
                ),
                "campus_registrations.batch_year": FieldDef(
                    "int", "Campus batch year", "none", lambda t: t.batch_year, frozenset({"eq", "in"}), join="campus_registrations"
                ),
                "campus_registrations.drive_id": FieldDef(
                    "uuid", "Campus drive", "none", lambda t: t.drive_id, frozenset({"eq", "in"}), join="campus_registrations"
                ),
                "campus_registrations.status": FieldDef(
                    "enum", "Campus registration status", "none", lambda t: t.status, frozenset({"eq", "in"}),
                    enum_values=REGISTRATION_STATUSES, join="campus_registrations",
                ),
            },
            joins={"campus_registrations": JoinDef(Registration, lambda c, t: t.candidate_id == c.id, many=True)},
        )
