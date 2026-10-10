# ruff: noqa: E501, ARG001
"""Assessment assembly and publishing (Epic 40 A4, FR-202).

* Assessments are edited as a draft (``draft_sections``: fixed ``item_id`` lists or draw rules).
* ``publish`` validates and freezes into an immutable ``assessment_versions`` row: every fixed item's CURRENT version
  id; for draw sections, the candidate pool of item-version ids matching the rule at publish time (pool >= count,
  else 422 ``draw_pool_too_small``; pool < 2 x count is a warning); total points > 0. Later item edits never change
  a published version. Provider delivery needs ``provider_config_id`` + ``provider_test_ref`` and has no sections.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.assessments import (
    Assessment,
    AssessmentItem,
    AssessmentItemVersion,
    AssessmentProviderTest,
    AssessmentVersion,
)
from app.schemas.assessments import (
    AssessmentCreate,
    AssessmentDetailOut,
    AssessmentOut,
    AssessmentPatch,
    DraftIn,
    PublishOut,
    VersionBrief,
    VersionOut,
    VersionSectionOut,
)
from app.services.assessments.common import conflict, invalid, not_found, now, tenant_of, user_id
from app.services.assessments.items import _skill_keys


async def load(db: AsyncSession, assessment_id: uuid.UUID, *, for_update: bool = False) -> Assessment:
    stmt = select(Assessment).where(Assessment.id == assessment_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


def section_summary(section: dict[str, Any]) -> VersionSectionOut:
    if section.get("draw"):
        pool = section.get("pool") or []
        count = int(section["draw"]["count"])
        mean = (sum(Decimal(str(p["points"])) for p in pool) / len(pool)) if pool else Decimal(0)
        return VersionSectionOut(
            key=section["key"], title=section["title"], time_limit_minutes=section.get("time_limit_minutes"),
            kind="draw", item_count=count, points=(mean * count).quantize(Decimal("0.01")), pool_size=len(pool),
        )  # fmt: skip
    entries = section.get("items") or []
    return VersionSectionOut(
        key=section["key"], title=section["title"], time_limit_minutes=section.get("time_limit_minutes"),
        kind="fixed", item_count=len(entries), points=sum((Decimal(str(e["points"])) for e in entries), Decimal(0)),
        pool_size=None,
    )  # fmt: skip


def version_out(v: AssessmentVersion) -> VersionOut:
    return VersionOut(
        id=v.id,
        version=v.version,
        published_at=v.published_at,
        published_by=v.published_by,
        total_points=v.total_points,
        sections=[section_summary(s) for s in v.sections or []],
    )


async def _version_no(db: AsyncSession, version_id: uuid.UUID | None) -> int | None:
    if version_id is None:
        return None
    return (
        await db.execute(select(AssessmentVersion.version).where(AssessmentVersion.id == version_id))
    ).scalar_one_or_none()


def _base(a: Assessment, version_no: int | None) -> dict[str, Any]:
    return {
        "id": a.id, "name": a.name, "description": a.description, "status": a.status, "delivery": a.delivery,
        "owner_id": a.owner_id, "time_limit_minutes": a.time_limit_minutes, "pass_mark_pct": a.pass_mark_pct,
        "gating_default": a.gating_default, "current_version_id": a.current_version_id,
        "current_version_no": version_no, "created_at": a.created_at, "updated_at": a.updated_at,
    }  # fmt: skip


async def detail(db: AsyncSession, a: Assessment) -> AssessmentDetailOut:
    versions = list(
        (
            await db.execute(
                select(AssessmentVersion)
                .where(AssessmentVersion.assessment_id == a.id)
                .order_by(AssessmentVersion.version.desc())
            )
        ).scalars()
    )
    current = next((v for v in versions if v.id == a.current_version_id), None)
    return AssessmentDetailOut(
        **_base(a, current.version if current else None),
        instructions=a.instructions,
        shuffle_items=a.shuffle_items,
        shuffle_options=a.shuffle_options,
        attempts_limit=a.attempts_limit,
        auto_pass_threshold_pct=a.auto_pass_threshold_pct,
        skill_keys=list(a.skill_keys or []),
        provider_config_id=a.provider_config_id,
        provider_test_ref=a.provider_test_ref,
        draft_sections=list(a.draft_sections or []),
        current_version=version_out(current) if current else None,
        versions=[
            VersionBrief(id=v.id, version=v.version, published_at=v.published_at, total_points=v.total_points)
            for v in versions
        ],
    )


async def list_assessments(
    ctx: RequestContext, db: AsyncSession, status: str | None = None
) -> list[AssessmentOut]:
    stmt = select(Assessment, AssessmentVersion.version).outerjoin(
        AssessmentVersion, AssessmentVersion.id == Assessment.current_version_id
    )
    if status:
        stmt = stmt.where(Assessment.status == status)
    rows = (await db.execute(stmt.order_by(Assessment.updated_at.desc()).limit(500))).all()
    return [AssessmentOut(**_base(a, no)) for a, no in rows]


async def get(ctx: RequestContext, db: AsyncSession, assessment_id: uuid.UUID) -> AssessmentDetailOut:
    return await detail(db, await load(db, assessment_id))


def _check_provider(delivery: str, config_id: uuid.UUID | None, test_ref: str | None) -> None:
    if delivery == "provider" and (config_id is None or not test_ref):
        raise invalid(
            "provider_test_required",
            "Provider delivery needs a connector and a provider test",
            "provider_test_ref",
        )


async def create(ctx: RequestContext, db: AsyncSession, data: AssessmentCreate) -> AssessmentDetailOut:
    owner = user_id(ctx)
    if owner is None:
        raise invalid("owner_required", "Only a user can create assessments")
    fields = data.model_dump(exclude={"skill_keys"})
    row = Assessment(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        owner_id=owner,
        status="draft",
        skill_keys=_skill_keys(data.skill_keys),
        draft_sections=[],
        **fields,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return await detail(db, row)


async def update(
    ctx: RequestContext, db: AsyncSession, assessment_id: uuid.UUID, data: AssessmentPatch
) -> AssessmentDetailOut:
    a = await load(db, assessment_id, for_update=True)
    if a.status == "archived":
        raise conflict("assessment_archived", "Archived assessments cannot be edited")
    changes = data.model_dump(exclude_unset=True)
    if "skill_keys" in changes:
        changes["skill_keys"] = _skill_keys(changes["skill_keys"] or [])
    for key, value in changes.items():
        if (
            key
            in ("name", "delivery", "shuffle_items", "shuffle_options", "attempts_limit", "gating_default")
            and value is None
        ):
            continue
        setattr(a, key, value)
    await db.flush()
    await db.refresh(a)
    return await detail(db, a)


async def save_draft(
    ctx: RequestContext, db: AsyncSession, assessment_id: uuid.UUID, data: DraftIn
) -> AssessmentDetailOut:
    a = await load(db, assessment_id, for_update=True)
    if a.status == "archived":
        raise conflict("assessment_archived", "Archived assessments cannot be edited")
    keys = [s.key for s in data.sections]
    if len(set(keys)) != len(keys):
        raise invalid("duplicate_section", "Section keys must be unique", "sections")
    for i, s in enumerate(data.sections):
        if (s.items is None) == (s.draw is None):
            raise invalid(
                "invalid_section", "A section has either fixed items or a draw rule", f"sections[{i}]"
            )
        if s.draw is not None:
            s.draw.skill_keys = _skill_keys(s.draw.skill_keys)
    a.draft_sections = [s.model_dump(mode="json", exclude_none=True) for s in data.sections]
    await db.flush()
    await db.refresh(a)
    return await detail(db, a)


async def _freeze_sections(
    db: AsyncSession, sections: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], Decimal, list[str]]:
    warnings: list[str] = []
    frozen: list[dict[str, Any]] = []
    total = Decimal(0)
    if not sections:
        raise invalid("assessment_empty", "Add at least one section before publishing", "sections")
    current = (
        select(AssessmentItem, AssessmentItemVersion)
        .join(
            AssessmentItemVersion,
            (AssessmentItemVersion.item_id == AssessmentItem.id)
            & (AssessmentItemVersion.version == AssessmentItem.current_version),
        )
        .where(AssessmentItem.archived_at.is_(None))
    )
    for i, s in enumerate(sections):
        base = {"key": s["key"], "title": s["title"], "time_limit_minutes": s.get("time_limit_minutes")}
        if s.get("draw"):
            rule = s["draw"]
            lo, hi = sorted(rule.get("difficulty") or (1, 5))
            stmt = current.where(AssessmentItem.difficulty.between(lo, hi))
            if rule.get("skill_keys"):
                stmt = stmt.where(AssessmentItem.skill_keys.overlap(list(rule["skill_keys"])))
            if rule.get("tags"):
                stmt = stmt.where(AssessmentItem.tags.overlap(list(rule["tags"])))
            pool = [
                {
                    "item_version_id": str(v.id),
                    "item_id": str(it.id),
                    "difficulty": it.difficulty,
                    "points": str(v.points),
                }
                for it, v in (await db.execute(stmt.order_by(AssessmentItem.id))).all()
            ]
            count = int(rule["count"])
            if len(pool) < count:
                raise invalid(
                    "draw_pool_too_small", f"Section '{s['title']}' draws {count} items but only {len(pool)} match", f"sections[{i}].draw",
                    pool_size=len(pool),
                )  # fmt: skip
            if len(pool) < 2 * count:
                warnings.append(
                    f"Section '{s['title']}': the pool ({len(pool)}) is smaller than twice the draw count ({count})."
                )
            frozen.append({**base, "draw": rule, "pool": pool})
            total += section_summary(frozen[-1]).points
        else:
            refs = s.get("items") or []
            if not refs:
                raise invalid("section_empty", f"Section '{s['title']}' has no items", f"sections[{i}].items")
            ids = [uuid.UUID(str(r["item_id"])) for r in refs]
            found = {
                it.id: (it, v)
                for it, v in (await db.execute(current.where(AssessmentItem.id.in_(ids)))).all()
            }
            entries: list[dict[str, Any]] = []
            for r, item_id in zip(refs, ids, strict=True):
                if item_id not in found:
                    raise invalid(
                        "item_unavailable",
                        "An item is missing or archived",
                        f"sections[{i}].items",
                        item_id=str(item_id),
                    )
                _, v = found[item_id]
                entries.append(
                    {
                        "item_version_id": str(v.id),
                        "item_id": str(item_id),
                        "points": str(Decimal(str(r.get("points") or v.points))),
                    }
                )
            frozen.append({**base, "items": entries})
            total += sum((Decimal(e["points"]) for e in entries), Decimal(0))
    if total <= 0:
        raise invalid("assessment_no_points", "The assessment has no points", "sections")
    return frozen, total, warnings


async def publish(ctx: RequestContext, db: AsyncSession, assessment_id: uuid.UUID) -> PublishOut:
    a = await load(db, assessment_id, for_update=True)
    if a.status == "archived":
        raise conflict("assessment_archived", "Archived assessments cannot be published")
    warnings: list[str] = []
    if a.delivery == "provider":
        _check_provider(a.delivery, a.provider_config_id, a.provider_test_ref)
        sections: list[dict[str, Any]] = []
        total = Decimal(100)
    else:
        sections, total, warnings = await _freeze_sections(db, list(a.draft_sections or []))
    last = (
        await db.execute(
            select(func.max(AssessmentVersion.version)).where(AssessmentVersion.assessment_id == a.id)
        )
    ).scalar_one_or_none()
    version = AssessmentVersion(
        id=uuid.uuid4(), tenant_id=a.tenant_id, assessment_id=a.id, version=(last or 0) + 1, published_by=user_id(ctx),
        published_at=now(), total_points=total, time_limit_minutes=a.time_limit_minutes, sections=sections,
    )  # fmt: skip
    db.add(version)
    await db.flush()
    a.current_version_id = version.id
    a.status = "published"
    await db.flush()
    return PublishOut(version=version_out(version), warnings=warnings)


async def archive(ctx: RequestContext, db: AsyncSession, assessment_id: uuid.UUID) -> AssessmentDetailOut:
    a = await load(db, assessment_id, for_update=True)
    if a.status != "archived":
        a.status = "archived"
        a.archived_at = now()
        await db.flush()
    await db.refresh(a)
    return await detail(db, a)


async def provider_tests(
    ctx: RequestContext, db: AsyncSession, config_id: uuid.UUID | None = None
) -> list[AssessmentProviderTest]:
    stmt = select(AssessmentProviderTest)
    if config_id is not None:
        stmt = stmt.where(AssessmentProviderTest.config_id == config_id)
    return list((await db.execute(stmt.order_by(AssessmentProviderTest.name))).scalars())


async def quick_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[Any]:
    """Epic 52 global search (foundation 0.7): assessments by name (``QuickHit`` type ``assessment``)."""
    from app.schemas.search import QuickHit

    term = (q or "").strip()[:100]
    if not term:
        return []
    like = f"%{term.replace('%', r'\%').replace('_', r'\_')}%"
    rows = (
        await db.execute(
            select(Assessment)
            .where(Assessment.name.ilike(like))
            .order_by(Assessment.updated_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [
        QuickHit(type="assessment", id=a.id, title=a.name, subtitle=a.status, url=f"/assessments/{a.id}")
        for a in rows
    ]


__all__ = [
    "archive",
    "create",
    "detail",
    "get",
    "list_assessments",
    "load",
    "provider_tests",
    "publish",
    "quick_search",
    "save_draft",
    "update",
    "version_out",
]
