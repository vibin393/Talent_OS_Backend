# ruff: noqa: E501
"""Seed Pool templates (Epic 38 story 38.4, FR-193). Disabled by default: nothing is created automatically.

``TEMPLATES`` is code-defined (section 0.6 field keys). Enabling one (``pool:manage``) creates a tenant-visible
Segment owned by the caller and a dynamic Pool with ``template_key``, then runs the first refresh; both stay
editable. A template is *available* when every field it needs is registered on its source -- ``campus_<batch>``
waits for Epic 43's ``campus_batch`` field (``extend_semantic_source``).

[ASSUMPTION, decision 16] Silver Medalists = reached ``selected`` / ``offered`` within 548 days (~18 months) and
not placed within 548 days; Former Workers = ended Placements not offboarded for misconduct (P2 has no rehire flag).
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.semantic import get_source
from app.models.talent_pools import TalentPool
from app.schemas.segments import SegmentCreate
from app.schemas.talent_pools import PoolCreate, TemplateOut
from app.services.segments import crud as segments_crud
from app.services.segments import semantic_fields
from app.services.talent_pools import pools

CAMPUS_PREFIX = "campus_"
CAMPUS_FIELD = "campus_batch"
SILVER_DAYS = 548


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    source_key: str
    filters: dict[str, Any]
    requires_fields: tuple[str, ...] = field(default=())


TEMPLATES: dict[str, Template] = {
    "immediate_joiners": Template(
        "Immediate Joiners",
        "Candidates who can join within 15 days.",
        "candidates.candidate",
        {
            "or": [
                {"field": "notice_period_days", "op": "lte", "value": 15},
                {
                    "field": "availability_date",
                    "op": "within_days",
                    "value": {"days": 15, "direction": "future"},
                },
            ]
        },
        ("notice_period_days", "availability_date"),
    ),
    "silver_medalists": Template(
        "Silver Medalists",
        "Reached the selected or offer stage in the last 18 months but were not placed.",
        "candidates.candidate",
        {
            "and": [
                {"field": "stage_history.stage_key", "op": "in", "value": ["selected", "offered"]},
                {"field": "stage_history.entered_at", "op": "within_days", "value": SILVER_DAYS},
                {
                    "or": [
                        {"field": "days_since_last_placement", "op": "is_null", "value": True},
                        {"field": "days_since_last_placement", "op": "gt", "value": SILVER_DAYS},
                    ]
                },
            ]
        },
        ("stage_history.stage_key", "stage_history.entered_at", "days_since_last_placement"),
    ),
    "former_workers": Template(
        "Former Workers",
        "Workers whose placement ended, except endings for misconduct.",
        "placements.worker",
        {
            "and": [
                {"field": "status", "op": "eq", "value": "ended"},
                {"field": "offboarding.reason", "op": "not_in", "value": ["misconduct"]},
            ]
        },
        ("status", "offboarding.reason"),
    ),
    "contractors_ending": Template(
        "Contractors Ending Soon",
        "Active workers whose placement ends in the next 60 days.",
        "placements.worker",
        {
            "and": [
                {"field": "status", "op": "eq", "value": "active"},
                {
                    "field": "effective_end_date",
                    "op": "within_days",
                    "value": {"days": 60, "direction": "future"},
                },
            ]
        },
        ("status", "effective_end_date"),
    ),
    "bench": Template(
        "Bench",
        "Candidates on the bench or proposed from it.",
        "candidates.candidate",
        {"field": "bench.status", "op": "in", "value": ["on_bench", "proposed"]},
        ("bench.status",),
    ),
    "campus": Template(
        "Campus batch",
        "Candidates registered from a campus batch (enable with the batch, e.g. campus_2027).",
        "candidates.candidate",
        {"field": CAMPUS_FIELD, "op": "eq", "value": "<batch>"},
        (CAMPUS_FIELD,),
    ),
}


def missing_fields(t: Template) -> list[str]:
    semantic_fields.register()
    try:
        fields = get_source(t.source_key).fields
    except KeyError:
        return list(t.requires_fields)
    return [f for f in t.requires_fields if f not in fields]


def _resolve(key: str, batch: str | None) -> tuple[str, Template, str | None]:
    if key.startswith(CAMPUS_PREFIX):
        return key, TEMPLATES["campus"], key.removeprefix(CAMPUS_PREFIX)
    if key == "campus":
        if not batch:
            raise ValidationFailed("Name the campus batch", code="batch_required", details={"path": "batch"})
        return f"{CAMPUS_PREFIX}{batch}", TEMPLATES["campus"], batch
    template = TEMPLATES.get(key)
    if template is None:
        raise NotFound("Template not found")
    return key, template, None


def _filters(t: Template, batch: str | None) -> dict[str, Any]:
    if batch is None:
        return t.filters
    return {**t.filters, "value": batch}


async def list_templates(ctx: RequestContext, db: AsyncSession) -> list[TemplateOut]:  # noqa: ARG001
    rows = (
        await db.execute(
            select(TalentPool.template_key, TalentPool.id).where(
                TalentPool.template_key.is_not(None), TalentPool.status == "active"
            )
        )
    ).all()
    enabled: dict[str, uuid.UUID] = {str(k): pid for k, pid in rows}
    out = []
    for key, t in TEMPLATES.items():
        missing = missing_fields(t)
        out.append(
            TemplateOut(
                key=key,
                name=t.name,
                description=t.description,
                source_key=t.source_key,
                enabled=key in enabled
                if key != "campus"
                else any(k.startswith(CAMPUS_PREFIX) for k in enabled),
                available=not missing,
                unavailable_reason=(
                    "Campus batches arrive with the Campus module"
                    if key == "campus" and missing
                    else (
                        f"Needs fields that are not available yet: {', '.join(missing)}" if missing else None
                    )
                ),
                pool_id=enabled.get(key),
            )
        )
    return out


async def enable(ctx: RequestContext, db: AsyncSession, key: str, batch: str | None) -> TalentPool:
    template_key, t, batch_value = _resolve(key, batch)
    if missing_fields(t):
        raise ConflictError("This template is not available yet", code="template_unavailable")
    existing = (
        await db.execute(
            select(TalentPool).where(TalentPool.template_key == template_key, TalentPool.status == "active")
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "This template is already enabled", code="template_enabled", details={"pool_id": str(existing.id)}
        )
    name = t.name if batch_value is None else f"{t.name} {batch_value}"
    await pools.check_dynamic_limit(ctx, db)
    segment = await segments_crud.create_segment(
        ctx,
        db,
        SegmentCreate(
            name=name,
            description=t.description,
            source_key=t.source_key,
            definition={"filters": _filters(t, batch_value)},
            visibility="tenant",
        ),
        owner_visibility_checked=True,
    )
    # create_pool requests the first refresh (trigger "created").
    return await pools.create_pool(
        ctx,
        db,
        PoolCreate(
            name=name, description=t.description, kind="dynamic", visibility="tenant", segment_id=segment.id
        ),
        template_key=template_key,
    )


def template_keys() -> list[str]:
    return list(TEMPLATES)


__all__ = ["TEMPLATES", "Template", "enable", "list_templates", "missing_fields", "template_keys"]
