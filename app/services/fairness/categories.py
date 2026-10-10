# ruff: noqa: E501
"""Category sets (Epic 49 A1, FR-246): the platform default set (gender, age band, disability -- never caste, religion
or race/ethnicity), lazy per-tenant seeding, label edits (a new set version) and answer validation.

Answers are ``{dimension_key: option_key}``; a missing or ``prefer_not_to_say`` answer counts as *unknown*. Age bands
are chosen by the Candidate -- nothing is ever derived from a date of birth or inferred from a name or photo.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fairness import DIMENSION_KEYS, PREFER_NOT, DemographicCategorySet, DemographicDimension
from app.services.fairness.common import invalid

DEFAULT_SET_KEY = "default"
DEFAULT_DIMENSIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "gender",
        "Gender",
        (
            ("woman", "Woman"),
            ("man", "Man"),
            ("non_binary", "Non-binary"),
            ("self_describe_other", "I describe myself another way"),
            (PREFER_NOT, "Prefer not to say"),
        ),
    ),
    (
        "age_band",
        "Age band",
        (
            ("under_25", "Under 25"),
            ("25_34", "25-34"),
            ("35_44", "35-44"),
            ("45_54", "45-54"),
            ("55_plus", "55 or over"),
            (PREFER_NOT, "Prefer not to say"),
        ),
    ),
    ("disability", "Disability", (("yes", "Yes"), ("no", "No"), (PREFER_NOT, "Prefer not to say"))),
)
OPTION_KEYS: dict[str, tuple[str, ...]] = {k: tuple(o for o, _ in opts) for k, _, opts in DEFAULT_DIMENSIONS}
OPTION_LABELS: dict[str, dict[str, str]] = {k: dict(opts) for k, _, opts in DEFAULT_DIMENSIONS}
DIMENSION_LABELS: dict[str, str] = {k: label for k, label, _ in DEFAULT_DIMENSIONS}
if tuple(OPTION_KEYS) != DIMENSION_KEYS:  # pragma: no cover - import-time guard
    msg = "default dimensions drifted from the DB CHECK"
    raise RuntimeError(msg)


async def active_set(db: AsyncSession) -> DemographicCategorySet:
    """Return the tenant's active category set, seeded from the platform default on first use."""
    row = (
        await db.execute(
            select(DemographicCategorySet)
            .where(DemographicCategorySet.key == DEFAULT_SET_KEY, DemographicCategorySet.status == "active")
            .order_by(DemographicCategorySet.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    latest = (
        await db.execute(
            select(DemographicCategorySet.version)
            .where(DemographicCategorySet.key == DEFAULT_SET_KEY)
            .order_by(DemographicCategorySet.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    row = DemographicCategorySet(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        key=DEFAULT_SET_KEY,
        name="Default categories",
        version=(latest or 0) + 1,
        status="active",
        is_default=True,
    )
    db.add(row)
    await db.flush()
    for pos, (key, label, opts) in enumerate(DEFAULT_DIMENSIONS, start=1):
        db.add(
            DemographicDimension(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                set_id=row.id,
                key=key,
                label=label,
                enabled=True,
                position=pos,
                options=[{"key": o, "label": lbl} for o, lbl in opts],
            )
        )
    await db.flush()
    return row


async def dimensions(db: AsyncSession, set_id: uuid.UUID) -> list[DemographicDimension]:
    return list(
        (
            await db.execute(
                select(DemographicDimension)
                .where(DemographicDimension.set_id == set_id)
                .order_by(DemographicDimension.position)
            )
        ).scalars()
    )


def dimension_out(d: DemographicDimension) -> dict[str, Any]:
    return {
        "key": d.key,
        "label": d.label,
        "enabled": d.enabled,
        "position": d.position,
        "options": [{"key": str(o["key"]), "label": str(o["label"])} for o in d.options or []],
    }


def validate_answers(dims: list[DemographicDimension], answers: Mapping[str, Any]) -> dict[str, str]:
    """``{dimension: option}`` limited to enabled dimensions of the set; anything else -> 422. Missing enabled
    dimensions default to ``prefer_not_to_say``."""
    enabled = {d.key: d for d in dims if d.enabled}
    out: dict[str, str] = {}
    for key, value in (answers or {}).items():
        if key not in enabled:
            raise invalid(
                "self_id_unknown_dimension", "This question is not part of the self-identification form", key
            )
        allowed = {str(o["key"]) for o in enabled[key].options or []}
        if not isinstance(value, str) or value not in allowed:
            raise invalid("self_id_invalid_option", "Choose one of the listed answers", key)
        out[key] = value
    for key in enabled:
        out.setdefault(key, PREFER_NOT)
    return out


async def update_labels(db: AsyncSession, edits: list[Mapping[str, Any]]) -> DemographicCategorySet:
    """Relabel / enable / disable dimensions and options. Creates a new set version (existing answers keep their
    ``set_version``). Option keys and dimensions cannot be added -- unknown keys -> 422."""
    current = await active_set(db)
    dims = {d.key: d for d in await dimensions(db, current.id)}
    by_key: dict[str, Mapping[str, Any]] = {}
    for e in edits:
        key = str(e.get("key"))
        if key not in DIMENSION_KEYS or key not in dims:
            raise invalid(
                "self_id_dimension_not_allowed",
                "Only gender, age band and disability can be collected",
                "key",
            )
        by_key[key] = e
    tenant_id = current.tenant_id
    new_dims: list[DemographicDimension] = []
    new_id = uuid.uuid4()
    for key, d in dims.items():
        e = by_key.get(key, {})
        labels = {str(o["key"]): str(o["label"]) for o in d.options or []}
        for o in e.get("options") or []:
            okey = str(o.get("key"))
            if okey not in labels:
                raise invalid("self_id_option_not_allowed", "Answer options cannot be added", "options")
            label = str(o.get("label") or "").strip()
            if not label or len(label) > 200:
                raise invalid(
                    "self_id_invalid_label", "Each answer needs a label (max 200 characters)", "options"
                )
            labels[okey] = label
        label = str(e.get("label") or d.label).strip()[:200] or d.label
        new_dims.append(
            DemographicDimension(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                set_id=new_id,
                key=key,
                label=label,
                enabled=bool(e.get("enabled", d.enabled)),
                position=d.position,
                options=[{"key": k, "label": v} for k, v in labels.items()],
            )
        )
    current.status = "retired"
    await db.flush()
    new = DemographicCategorySet(
        id=new_id,
        tenant_id=tenant_id,
        key=current.key,
        name=current.name,
        version=current.version + 1,
        status="active",
        is_default=current.is_default,
    )
    db.add(new)
    await db.flush()
    db.add_all(new_dims)
    await db.flush()
    return new
